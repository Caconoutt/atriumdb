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

"""Dashboard-layer helpers that sit above the SQL handler.

This module provides two things:

1. ``LOCATION_LOOKUP`` — translates API location codes (e.g. ``"ICU"``) to
   the exact ``unit.name`` strings stored in the database.

2. ``query_patient_encounters`` — translates location codes then delegates
   the actual JOIN query (``encounter → bed → unit``) to
   :meth:`~atriumdb.sql_handler.sql_handler.SQLHandler.select_patient_encounters`,
   which is implemented by both ``SQLiteHandler`` and ``MariaDBHandler``.
   Only runs in direct-DB mode (``metadata_connection_type`` of
   ``"sqlite"``, ``"mysql"``, or ``"mariadb"``).

3. ``group_encounters_by_visit`` — pure Python; collapses per-``encounter``
   rows from the SQL handler into per-visit admission records.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

logger = logging.getLogger(__name__)

# Maps API location codes to the exact strings stored in unit.name.
# Filtering is performed against unit.name (not unit.type).
# Populate by running: SELECT DISTINCT name FROM unit;
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],
    "OR": ["OR"],
}


def query_patient_encounters(
    sdk: "AtriumSDK",
    patient_id_list: list[int] | None = None,
    admit_start_ns: int | None = None,
    admit_end_ns: int | None = None,
    locations: list[str] | None = None,
) -> list[dict]:
    """Query encounters joined to bed and unit for dashboard cohort resolution.

    Translates API location codes to ``unit.name`` values via
    ``LOCATION_LOOKUP``, then delegates the actual SQL to
    :meth:`~atriumdb.sql_handler.sql_handler.SQLHandler.select_patient_encounters`
    which is implemented by both ``SQLiteHandler`` and ``MariaDBHandler``.

    Returns one dict per ``encounter`` row (not per visit). Pass the result to
    :func:`group_encounters_by_visit` to collapse rows into per-visit
    admission records.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param patient_id_list: Restrict to these internal patient IDs.
        ``None`` applies no patient filter.
    :param admit_start_ns: Lower bound on ``encounter.start_time`` (inclusive),
        epoch nanoseconds. ``None`` means no lower bound.
    :param admit_end_ns: Upper bound on ``encounter.start_time`` (inclusive),
        epoch nanoseconds. ``None`` means no upper bound.
    :param locations: API location codes, e.g. ``["ICU"]``. Resolved to
        ``unit.name`` values via ``LOCATION_LOOKUP`` before being passed to
        the SQL handler. ``None`` means no location filter.
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
    :raises ValueError: If a location code is not present in ``LOCATION_LOOKUP``.
    """
    unit_name_list = None
    if locations:
        unit_name_list = []
        for code in locations:
            if code not in LOCATION_LOOKUP:
                raise ValueError(
                    f"Unknown location code {code!r}. "
                    f"Valid codes are: {list(LOCATION_LOOKUP)}"
                )
            unit_name_list.extend(LOCATION_LOOKUP[code])

    rows = sdk.sql_handler.select_patient_encounters(
        patient_id_list=patient_id_list,
        admit_start_ns=admit_start_ns,
        admit_end_ns=admit_end_ns,
        unit_name_list=unit_name_list,
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


def group_encounters_by_visit(
    encounter_rows: list[dict],
) -> dict[tuple[int, str | None], dict]:
    """Collapse per-encounter rows into per-visit admission records.

    A hospital stay can produce multiple ``encounter`` rows sharing the same
    ``visit_number`` (one row per bed/unit transfer). This function groups
    those rows into a single visit record per ``(patient_id, visit_number)``
    pair using the rules from design doc Assumption §7:

    - ``admit_time_ns``  = MIN(``start_time``) across the visit's rows
    - ``discharge_time_ns`` = MAX(``end_time``); ``None`` if any row is still
      open (i.e. ``end_time`` is NULL), meaning the stay is ongoing.

    Rows with ``visit_number = None`` are grouped under ``(patient_id, None)``
    and aggregated with the same MIN/MAX rules as any other visit. A warning
    is logged for each NULL ``visit_number`` encountered, as this indicates
    incomplete data.

    :param encounter_rows: Output of :func:`query_patient_encounters` — one
        dict per ``encounter`` row.
    :return: Dict keyed by ``(patient_id, visit_number)``::

            {
                (patient_id, visit_number): {
                    "patient_id":        int,
                    "visit_number":      str | None,
                    "admit_time_ns":     int,
                    "discharge_time_ns": int | None,
                    "unit_names":        set[str],
                }
            }
    """
    visits: dict[tuple[int, str | None], dict] = {}

    for row in encounter_rows:
        pid = row["patient_id"]
        vn = row["visit_number"]
        key = (pid, vn)

        if vn is None:
            logger.warning(
                "Encountered NULL visit_number for patient_id=%s "
                "(encounter_id=%s) — indicates incomplete data.",
                pid,
                row["encounter_id"],
            )

        if key not in visits:
            visits[key] = {
                "patient_id":        pid,
                "visit_number":      vn,
                "admit_time_ns":     row["start_time_ns"],
                "discharge_time_ns": row["end_time_ns"],
                "unit_names":        {row["unit_name"]} if row["unit_name"] else set(),
            }
        else:
            v = visits[key]
            if row["start_time_ns"] < v["admit_time_ns"]:
                v["admit_time_ns"] = row["start_time_ns"]
            # discharge_time = MAX(end_time); None if any row is still open
            if v["discharge_time_ns"] is None or row["end_time_ns"] is None:
                v["discharge_time_ns"] = None
            elif row["end_time_ns"] > v["discharge_time_ns"]:
                v["discharge_time_ns"] = row["end_time_ns"]
            if row["unit_name"]:
                v["unit_names"].add(row["unit_name"])

    return visits
