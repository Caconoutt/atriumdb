#!/usr/bin/env python3
"""Scratch query runner - edit the block between the two markers, nothing else.

    python queries/query_template.py

The database is opened read-only, so a stray UPDATE/DELETE fails instead of
touching the real data.
"""
import os
import sqlite3

DB = os.environ.get("ATRIUMDB_INDEX_DB", "/data/atriumdb/meta/index.db")

# Handy constants for epoch-nanosecond columns (dob, start_time, start_time_n, ...)
SECOND_NS = 1_000_000_000
HOUR_NS = 3600 * SECOND_NS
DAY_NS = 24 * HOUR_NS
MONTH_NS = 30 * DAY_NS
YEAR_NS = 365 * DAY_NS
YEAR_2020 = 1577836800000000000
YEAR_2021 = 1609459200000000000
YEAR_2022 = 1640995200000000000
YEAR_2023 = 1672531200000000000
YEAR_2024 = 1704067200000000000
YEAR_2025 = 1735689600000000000
YEAR_2026 = 1767225600000000000

min_age_ns = 2 * YEAR_NS
max_age_ns = 5 * YEAR_NS

def to_ns(date_str: str) -> int:
    """'2020-01-01' or '2020-01-01 13:45:00' (UTC) -> epoch nanoseconds."""
    from datetime import datetime, timezone

    fmt = "%Y-%m-%d %H:%M:%S" if " " in date_str else "%Y-%m-%d"
    dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * SECOND_NS


# ============================== EDIT BELOW ==============================
SQL = """
    SELECT p.*, e.start_time
    FROM encounter e
    JOIN patient p ON e.patient_id = p.id
    WHERE e.start_time >= ?
      AND e.start_time <= ?
      AND (e.start_time - p.dob) >= ?
      AND (e.start_time - p.dob) <= ?

"""

PARAMS = (YEAR_2021, YEAR_2024, min_age_ns, max_age_ns)

LIMIT_PRINT = 50  # only print the first N rows
# ============================== EDIT ABOVE ==============================


conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute(SQL, PARAMS).fetchall()

print("row count:", len(rows))
if rows:
    print("columns:", list(rows[0].keys()))
for row in rows[:LIMIT_PRINT]:
    print(dict(row))
if len(rows) > LIMIT_PRINT:
    print(f"... {len(rows) - LIMIT_PRINT} more rows not shown")
