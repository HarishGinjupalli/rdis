"""
db_utils.py
-----------
All database access for rDIS goes through this module.

Design goals (so the app can hold 70,00,000+ rows without crashing):
  1. Connection pooling via SQLAlchemy -> no "connection storm" when
     many Streamlit sessions/users hit the DB at once.
  2. Every DB call is wrapped with a retry decorator -> a dropped
     network blip does not crash the dashboard, it just retries.
  3. NOTHING pulls the full Inspections table into memory. All reads
     are either paginated (LIMIT/OFFSET style) or pre-aggregated.
  4. Bulk inserts use executemany with fast_executemany=True, which is
     10-50x faster than row-by-row inserts and won't time out on
     large batches.
"""

import time
import logging
from functools import wraps
from contextlib import contextmanager

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rDIS.db")

# ------------------------------------------------------------------
# Connection settings — edit for your SQL Server instance
# ------------------------------------------------------------------
SERVER = "DESKTOP-I43CQ5Q\MSSQLSERVER01"      # or your server name
DATABASE = "rdis_db"
DRIVER = "ODBC+Driver+17+for+SQL+Server"
USE_WINDOWS_AUTH = True                # set False and fill below if using SQL auth
SQL_USERNAME = "DESKTOP-I43CQ5Q\dell"
SQL_PASSWORD = ""

if USE_WINDOWS_AUTH:
    CONN_STR = (
        f"mssql+pyodbc://@{SERVER}/{DATABASE}"
        f"?driver={DRIVER}&trusted_connection=yes"
    )
else:
    CONN_STR = (
        f"mssql+pyodbc://{SQL_USERNAME}:{SQL_PASSWORD}@{SERVER}/{DATABASE}"
        f"?driver={DRIVER}"
    )

# A single, shared, pooled engine for the whole app process.
# pool_size / max_overflow keep us from ever opening unlimited connections
# even if many Streamlit users are browsing the dashboard simultaneously.
engine = create_engine(
    CONN_STR,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_pre_ping=True,     # detects a dead connection and replaces it silently
    fast_executemany=True,  # huge speedup for bulk inserts
)


def retry(times=3, delay_seconds=2):
    """Retry a DB call a few times before giving up, instead of crashing
    the whole app on a transient network/DB hiccup."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "DB call '%s' failed (attempt %d/%d): %s",
                        func.__name__, attempt, times, e,
                    )
                    time.sleep(delay_seconds)
            logger.error("DB call '%s' failed after %d attempts", func.__name__, times)
            raise last_err
        return wrapper
    return decorator


@contextmanager
def get_connection():
    """Context manager that always returns the connection to the pool,
    even if an exception happens inside the 'with' block."""
    conn = engine.connect()
    try:
        yield conn
    finally:
        conn.close()


# ------------------------------------------------------------------
# Reads — always paginated or pre-aggregated, never SELECT * on 70L rows
# ------------------------------------------------------------------

@retry()
def fetch_inspections_page(page: int = 1, page_size: int = 100,
                            status: str | None = None,
                            site_id: int | None = None,
                            date_from=None, date_to=None) -> pd.DataFrame:
    """Returns one page of inspection rows. Safe at any table size because
    it only ever pulls `page_size` rows using OFFSET/FETCH (SQL Server's
    native, index-friendly pagination)."""
    offset = (page - 1) * page_size

    filters = ["1=1"]
    params = {"offset": offset, "page_size": page_size}

    if status:
        filters.append("i.Status = :status")
        params["status"] = status
    if site_id:
        filters.append("i.SiteID = :site_id")
        params["site_id"] = site_id
    if date_from:
        filters.append("i.InspectionDate >= :date_from")
        params["date_from"] = date_from
    if date_to:
        filters.append("i.InspectionDate <= :date_to")
        params["date_to"] = date_to

    query = f"""
        SELECT i.InspectionID, i.InspectionDate, s.SiteName, ins.InspectorName,
               t.TemplateName, i.Status, i.RiskScore, i.Latitude, i.Longitude,
               i.CapturedOffline
        FROM dbo.Inspections i
        JOIN dbo.Sites s ON s.SiteID = i.SiteID
        JOIN dbo.Inspectors ins ON ins.InspectorID = i.InspectorID
        JOIN dbo.InspectionTemplates t ON t.TemplateID = i.TemplateID
        WHERE {' AND '.join(filters)}
        ORDER BY i.InspectionDate DESC
        OFFSET :offset ROWS FETCH NEXT :page_size ROWS ONLY
    """
    with get_connection() as conn:
        return pd.read_sql(text(query), conn, params=params)


@retry()
def fetch_kpis(date_from=None, date_to=None) -> dict:
    """Reads from the pre-aggregated DailySummary table (fast, constant time)
    rather than aggregating the raw 70L-row table on every dashboard load."""
    filters = ["1=1"]
    params = {}
    if date_from:
        filters.append("SummaryDate >= :date_from")
        params["date_from"] = date_from
    if date_to:
        filters.append("SummaryDate <= :date_to")
        params["date_to"] = date_to

    query = f"""
        SELECT
            SUM(TotalInspections) AS total_inspections,
            SUM(PassedCount)      AS passed,
            SUM(FailedCount)      AS failed,
            SUM(FlaggedCount)     AS flagged,
            AVG(AvgRiskScore)     AS avg_risk_score
        FROM dbo.DailySummary
        WHERE {' AND '.join(filters)}
    """
    with get_connection() as conn:
        row = conn.execute(text(query), params).mappings().first()
    return dict(row) if row else {}


@retry()
def fetch_trend(days: int = 30) -> pd.DataFrame:
    """Small, bounded result set (max `days` rows) for the trend chart —
    reads DailySummary, not the raw fact table."""
    query = """
        SELECT TOP (:days) SummaryDate, TotalInspections, PassedCount, FailedCount, FlaggedCount
        FROM dbo.DailySummary
        ORDER BY SummaryDate DESC
    """
    with get_connection() as conn:
        df = pd.read_sql(text(query), conn, params={"days": days})
    return df.sort_values("SummaryDate")


@retry()
def fetch_lookup(table: str) -> pd.DataFrame:
    """Small master tables (Sites, Inspectors, Templates) — safe to
    cache in the app since they rarely change."""
    allowed = {"Sites", "Inspectors", "InspectionTemplates"}
    if table not in allowed:
        raise ValueError(f"Unknown lookup table: {table}")
    with get_connection() as conn:
        return pd.read_sql(text(f"SELECT * FROM dbo.{table} WHERE IsActive = 1"), conn)


# ------------------------------------------------------------------
# Writes — batched, never row-by-row
# ------------------------------------------------------------------

@retry()
def insert_inspections_bulk(df: pd.DataFrame, chunksize: int = 1000):
    """Bulk-inserts a DataFrame of new inspections in chunks so a single
    huge insert (e.g. syncing 50,000 offline records) can't time out or
    exhaust memory. fast_executemany on the engine makes this fast."""
    total = len(df)
    for start in range(0, total, chunksize):
        chunk = df.iloc[start:start + chunksize]
        chunk.to_sql("Inspections", engine, schema="dbo", if_exists="append",
                     index=False, method="multi")
        logger.info("Inserted rows %d-%d of %d", start, start + len(chunk), total)


@retry()
def refresh_daily_summary(target_date=None):
    """Calls the stored procedure that recomputes today's KPI row.
    Run this after ingest batches (or on a schedule) instead of
    computing aggregates live in the dashboard."""
    with engine.begin() as conn:
        conn.execute(text("EXEC dbo.usp_RefreshDailySummary :d"), {"d": target_date})


@retry()
def health_check() -> bool:
    """Cheap query used by the dashboard to show a 'DB connected' /
    'DB unreachable' banner instead of crashing outright."""
    try:
        with get_connection() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return False
