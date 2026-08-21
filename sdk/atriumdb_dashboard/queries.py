# AtriumDB is a timeseries database software designed to best handle the unique features and
# challenges that arise from clinical waveform data.
#     Copyright (C) 2023  The Hospital for Sick Children
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Dashboard-layer encounter queries.

This module owns the full ``encounter → bed → unit`` query path, so that the
dashboard needs no changes to the ``atriumdb`` package. It provides three
things:

1. ``select_patient_encounters`` — the raw SQL. Runs against the SDK handler's
   ``connection()``, the upstream backend-agnostic context manager. Only runs
   in direct-DB mode (``metadata_connection_type`` of ``"sqlite"``,
   ``"mysql"``, or ``"mariadb"``).

2. ``query_patient_encounters`` — calls ``select_patient_encounters`` and
   shapes the rows into dicts.

3. ``group_encounters_by_admission`` — pure Python; collapses per-``encounter``
   rows into per-visit admission records.

Location strings are passed through to ``unit.name`` untranslated; validating
them against the database is :mod:`atriumdb_dashboard.locations`' job, and the
resolver does it before calling in here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "group_encounters_by_admission",
    "query_patient_encounters",
    "select_patient_encounters",
]

# The encounter → bed → unit join, shared by every direct-DB backend.
# Both the SQLite and MariaDB handlers use the ``?`` paramstyle, so one
# statement serves both.
_SELECT_PATIENT_ENCOUNTERS = (
    "SELECT e.id, e.patient_id, e.visit_number, e.bed_id, "
    "u.id, u.name, e.start_time, e.end_time "
    "FROM encounter e "
    "JOIN bed b ON e.bed_id = b.id "
    "JOIN unit u ON b.unit_id = u.id"
)

# Always applied, never optional. Admissions are keyed by
# (patient_id, visit_number, unit_name), so a row without a visit number cannot
# be attributed to a specific stay: every such row for a patient would collapse
# into one synthetic ``None`` admission spanning unrelated visits. Excluding
# them in SQL keeps that ambiguity out of the grouping entirely.
_VISIT_NUMBER_PRESENT = "e.visit_number IS NOT NULL"


def _handler_connection(sdk: "AtriumSDK"):
    """Return the SDK handler's connection context manager for direct-DB mode.

    ``SQLHandler.connection(begin)`` is the upstream backend-agnostic accessor:
    both ``SQLiteHandler`` and ``MariaDBHandler`` implement it by delegating to
    their own manager. Using it means this module needs no knowledge of which
    backend is in play and adds nothing to ``atriumdb``.

    :param sdk: AtriumSDK instance.
    :return: A context manager yielding ``(conn, cursor)``.
    :raises ValueError: If the SDK is in ``"api"`` mode, which has no local
        database to query.
    """
    if getattr(sdk, "metadata_connection_type", None) == "api":
        raise ValueError(
            "Encounter queries require direct database access; this SDK "
            "instance is in 'api' mode."
        )

    return sdk.sql_handler.connection(begin=False)


def select_patient_encounters(
    sdk: "AtriumSDK",
    patient_id_list: list[int] | None = None,
    admit_start_ns: int | None = None,
    admit_end_ns: int | None = None,
    unit_name_list: list[str] | None = None,
) -> list[tuple]:
    """Query encounters joined to bed and unit, returning raw rows.

    Two rows are excluded unconditionally, before any caller-supplied filter:

    - ``bed_id`` NULL, dropped by the INNER JOIN — a pre-admission placeholder
      without a bed assignment is not an admission.
    - ``visit_number`` NULL, dropped by an explicit WHERE clause — such a row
      cannot be attributed to a particular stay, and
      :func:`group_encounters_by_admission` keys admissions by
      ``(patient_id, visit_number, unit_name)``.

    A patient whose only encounters lack a visit number therefore resolves to
    no admissions at all, and drops out of a cohort. In the MRN path that
    surfaces in the "no encounter in date range" warning.

    Filtering is additive: all supplied arguments are AND-ed in the WHERE
    clause. Omitting an argument (leaving it ``None``) applies no filter for
    that dimension.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param patient_id_list: Restrict to these internal patient IDs.
        ``None`` applies no patient filter.
    :param admit_start_ns: Lower bound on ``encounter.start_time``
        (inclusive), epoch nanoseconds. ``None`` means no lower bound.
    :param admit_end_ns: Upper bound on ``encounter.start_time``
        (inclusive), epoch nanoseconds. ``None`` means no upper bound.
    :param unit_name_list: Restrict to encounters in units whose ``unit.name``
        is in this list. Values must already be resolved from API location
        codes by the caller. ``None`` applies no location filter.
    :return: List of tuples, one per matching ``encounter`` row, in ascending
        ``start_time`` order::

            (encounter_id, patient_id, visit_number, bed_id,
             unit_id, unit_name, start_time_ns, end_time_ns)

        ``end_time_ns`` is ``None`` when the stay is ongoing.
    """
    query = _SELECT_PATIENT_ENCOUNTERS
    where_clauses = [_VISIT_NUMBER_PRESENT]
    arg_tuple: tuple = ()

    if patient_id_list is not None and len(patient_id_list) > 0:
        where_clauses.append(
            "e.patient_id IN ({})".format(",".join(["?"] * len(patient_id_list)))
        )
        arg_tuple += tuple(int(pid) for pid in patient_id_list)

    if admit_start_ns is not None:
        where_clauses.append("e.start_time >= ?")
        arg_tuple += (int(admit_start_ns),)

    if admit_end_ns is not None:
        where_clauses.append("e.start_time <= ?")
        arg_tuple += (int(admit_end_ns),)

    if unit_name_list is not None and len(unit_name_list) > 0:
        where_clauses.append(
            "u.name IN ({})".format(",".join(["?"] * len(unit_name_list)))
        )
        arg_tuple += tuple(unit_name_list)

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += " ORDER BY e.start_time ASC"

    with _handler_connection(sdk) as (conn, cursor):
        cursor.execute(query, arg_tuple)
        return cursor.fetchall()


def query_patient_encounters(
    sdk: "AtriumSDK",
    patient_id_list: list[int] | None = None,
    admit_start_ns: int | None = None,
    admit_end_ns: int | None = None,
    locations: list[str] | None = None,
) -> list[dict]:
    """Query encounters joined to bed and unit for dashboard cohort resolution.

    Delegates the actual SQL to
    :func:`~atriumdb_dashboard.queries.select_patient_encounters`, which runs
    against the SDK's existing direct-DB connection.

    Returns one dict per ``encounter`` row (not per admission). Pass the result
    to :func:`group_encounters_by_admission` to collapse rows into per-admission
    records.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param patient_id_list: Restrict to these internal patient IDs.
        ``None`` applies no patient filter.
    :param admit_start_ns: Lower bound on ``encounter.start_time`` (inclusive),
        epoch nanoseconds. ``None`` means no lower bound.
    :param admit_end_ns: Upper bound on ``encounter.start_time`` (inclusive),
        epoch nanoseconds. ``None`` means no upper bound.
    :param locations: Location names matched directly against ``unit.name``,
        e.g. ``["ICU"]``. Validate them with
        :func:`~atriumdb_dashboard.locations.validate_location_codes` first;
        an unvalidated unknown name simply matches no rows. ``None`` means no
        location filter.
    :return: List of dicts, one per matching ``encounter`` row::

            {
                "encounter_id":  int,
                "patient_id":    int,
                "visit_number":  str | None,
                "bed_id":        int,
                "unit_id":       int,
                "unit_name":     str | None,
                "start_time_ns": int,
                "end_time_ns":   int | None,
            }
"""
    rows = select_patient_encounters(
        sdk,
        patient_id_list=patient_id_list,
        admit_start_ns=admit_start_ns,
        admit_end_ns=admit_end_ns,
        unit_name_list=locations or None,
    )

    return [
        {
            "encounter_id":  row[0],
            "patient_id":    row[1],
            "visit_number":  row[2],
            "bed_id":        row[3],
            "unit_id":       row[4],
            "unit_name":     row[5],
            "start_time_ns": row[6],
            "end_time_ns":   row[7],
        }
        for row in rows
    ]


def group_encounters_by_admission(
    encounter_rows: list[dict],
) -> dict[tuple[int, str | None, str | None], dict]:
    """Collapse per-encounter rows into per-admission records.

    A hospital stay produces one ``encounter`` row per bed, so a single visit
    can span many rows. Rows are grouped by ``(patient_id, visit_number,
    unit_name)``, which collapses bed-to-bed moves *within* a unit into one
    admission while keeping a transfer *between* units as two — each with its
    own admission and discharge times, and a single unambiguous location.

    That split is what lets a caller asking for several locations (e.g. ICU and
    OR) still see which unit each patient was actually in; a visit-level
    grouping could only report the set of units the stay touched.

    Within a group, per design doc Assumption §7:

    - ``admit_time_ns``  = MIN(``start_time``) across the group's rows
    - ``discharge_time_ns`` = MAX(``end_time``); ``None`` if any row is still
      open (i.e. ``end_time`` is NULL), meaning the stay is ongoing.

    Rows with ``visit_number = None`` do not arise from
    :func:`query_patient_encounters`, which excludes them in SQL. If passed in
    directly they are grouped under a ``None`` visit number and a warning is
    logged, since doing so merges what may be unrelated stays.

    :param encounter_rows: Output of :func:`query_patient_encounters` — one
        dict per ``encounter`` row.
    :return: Dict keyed by ``(patient_id, visit_number, unit_name)``::

            {
                (patient_id, visit_number, unit_name): {
                    "patient_id":        int,
                    "visit_number":      str | None,
                    "unit_name":         str | None,
                    "admit_time_ns":     int,
                    "discharge_time_ns": int | None,
                }
            }
    """
    admissions: dict[tuple[int, str | None, str | None], dict] = {}

    for row in encounter_rows:
        pid = row["patient_id"]
        vn = row["visit_number"]
        unit_name = row["unit_name"] or None
        key = (pid, vn, unit_name)

        if vn is None:
            # Unreachable via query_patient_encounters, which filters these out
            # in SQL. Retained because this function is public and may be given
            # rows from elsewhere; grouping them under a None visit number is
            # the safe fallback, but it merges unrelated stays, so say so.
            _LOGGER.warning(
                "Grouping an encounter with NULL visit_number for patient_id=%s "
                "(encounter_id=%s); it may merge unrelated stays. Rows from "
                "query_patient_encounters never hit this path.",
                pid,
                row["encounter_id"],
            )

        if key not in admissions:
            admissions[key] = {
                "patient_id":        pid,
                "visit_number":      vn,
                "unit_name":         unit_name,
                "admit_time_ns":     row["start_time_ns"],
                "discharge_time_ns": row["end_time_ns"],
            }
        else:
            a = admissions[key]
            if row["start_time_ns"] < a["admit_time_ns"]:
                a["admit_time_ns"] = row["start_time_ns"]
            # discharge_time = MAX(end_time); None if any row is still open
            if a["discharge_time_ns"] is None or row["end_time_ns"] is None:
                a["discharge_time_ns"] = None
            elif row["end_time_ns"] > a["discharge_time_ns"]:
                a["discharge_time_ns"] = row["end_time_ns"]

    return admissions
