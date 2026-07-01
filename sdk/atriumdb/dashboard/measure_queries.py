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

"""Dashboard-layer helpers for measure-level coverage statistics.

``interval_index`` holds one row per continuous stretch of data per
``(measure_id, device_id)`` pair. Rows within a pair are non-overlapping by
the SDK's write invariant (gaps inside a TSC block are stripped out by
``find_intervals()`` before writing), so a plain
``SUM(end_time_n - start_time_n) GROUP BY measure_id`` gives the true
data-coverage total without double-counting or gap inflation.

Only runs in direct-DB mode (``metadata_connection_type`` of ``"sqlite"``,
``"mysql"``, or ``"mariadb"``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

_NS_PER_HOUR = 3_600_000_000_000

_MEASURE_TOTAL_HOURS_SQL = """
    SELECT
        m.id                                                        AS measure_id,
        m.tag                                                       AS measure_tag,
        m.freq_nhz,
        m.unit                                                      AS units,
        COUNT(DISTINCT ii.device_id)                                AS num_devices,
        SUM(ii.end_time_n - ii.start_time_n)                       AS total_ns
    FROM interval_index ii
    JOIN measure m ON m.id = ii.measure_id
    GROUP BY ii.measure_id
    ORDER BY total_ns DESC
"""

_MEASURE_TOTAL_HOURS_KEYS = (
    "measure_id",
    "measure_tag",
    "freq_nhz",
    "units",
    "num_devices",
    "total_ns",
)


def query_measure_total_hours(sdk: "AtriumSDK") -> list[dict]:
    """Return true data-coverage hours per measure across all devices.

    Sums ``interval_index`` rows, which record only continuous stretches of
    actual data (intra-block gaps are already excluded at write time by
    ``find_intervals()``). This is the authoritative coverage figure.

    Returns an empty list when ``interval_index_mode="disable"`` was used
    during ingestion.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :return: List of dicts, one per measure, ordered by ``total_ns`` descending::

            {
                "measure_id":  int,
                "measure_tag": str | None,
                "freq_nhz":    int,
                "units":       str | None,
                "num_devices": int,
                "total_ns":    int,
                "total_hours": float,
            }
    """
    with sdk.sql_handler.connection(begin=False) as (conn, cursor):
        cursor.execute(_MEASURE_TOTAL_HOURS_SQL)
        rows = cursor.fetchall()

    result = []
    for row in rows:
        entry = dict(zip(_MEASURE_TOTAL_HOURS_KEYS, row))
        entry["total_hours"] = (entry["total_ns"] or 0) / _NS_PER_HOUR
        result.append(entry)

    _LOGGER.debug("%d measures queried for hours. ", len(result))
    return result
