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

Uses ``block_index`` to count stored samples per ``(measure_id, device_id)``
block, then converts to nanoseconds using the measure's sampling frequency
(``freq_nhz``, stored in nano-Hz).  The conversion is:

    period_ns  = 10^18 / freq_nhz        (since freq_nhz = Hz × 10^9)
    total_ns   = SUM(num_values) × period_ns

This gives the amount of *recorded data* in time units, aggregated across all
devices for each measure.

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
        m.id                          AS measure_id,
        m.tag                         AS measure_tag,
        m.freq_nhz,
        m.unit                        AS units,
        SUM(bi.num_values)            AS total_num_values
    FROM block_index bi
    JOIN measure m ON m.id = bi.measure_id
    WHERE m.freq_nhz > 0
    GROUP BY bi.measure_id
"""

_MEASURE_TOTAL_HOURS_KEYS = (
    "measure_id",
    "measure_tag",
    "freq_nhz",
    "units",
    "total_num_values",
)


def query_measure_total_hours(sdk: "AtriumSDK") -> list[dict]:
    """Return data-coverage hours per measure across all devices.

    Counts stored samples from ``block_index``, then converts to hours using
    each measure's ``freq_nhz``.  Measures with ``freq_nhz = 0`` (aperiodic /
    annotation signals) are excluded.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :return: List of dicts, one per measure, ordered by ``total_hours`` descending::

            {
                "measure_id":      int,
                "measure_tag":     str | None,
                "freq_nhz":        int,
                "units":           str | None,
                "total_num_values": int,
                "total_ns":        float,
                "total_hours":     float,
            }
    """
    with sdk.sql_handler.connection(begin=False) as (conn, cursor):
        cursor.execute(_MEASURE_TOTAL_HOURS_SQL)
        rows = cursor.fetchall()

    result = []
    for row in rows:
        entry = dict(zip(_MEASURE_TOTAL_HOURS_KEYS, row))
        total_num_values = entry["total_num_values"] or 0
        freq_nhz = entry["freq_nhz"] or 0
        # period_ns = 1e18 / freq_nhz  (freq_nhz = Hz × 1e9, so period_ns = 1e9/Hz = 1e18/freq_nhz)
        total_ns = total_num_values * 1e18 / freq_nhz if freq_nhz > 0 else 0.0
        entry["total_ns"] = total_ns
        entry["total_hours"] = total_ns / _NS_PER_HOUR
        result.append(entry)

    result.sort(key=lambda x: x["total_hours"], reverse=True)
    _LOGGER.debug("%d measures queried for hours.", len(result))
    return result
