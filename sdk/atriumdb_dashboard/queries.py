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

"""Dashboard-layer measure-coverage statistics.

Owns the whole query path so that the dashboard needs no changes to the
``atriumdb`` package:

1. ``select_measure_total_values`` — the raw SQL. Sums
   ``block_index.num_values`` per measure across every device, so gaps in
   acquisition are excluded rather than counted as coverage. Measures with
   ``freq_nhz = 0`` (aperiodic and annotation signals, where a sample count
   cannot be converted to a duration) are omitted. Runs against the SDK
   handler's ``connection()``, the upstream backend-agnostic context manager.

2. ``query_measure_total_hours`` — converts those counts to time units using
   each measure's sampling frequency (``freq_nhz``, stored in nano-Hz)::

       period_ns  = 10^18 / freq_nhz        (since freq_nhz = Hz x 10^9)
       total_ns   = SUM(num_values) x period_ns

Only runs in direct-DB mode (``metadata_connection_type`` of ``"sqlite"``,
``"mysql"``, or ``"mariadb"``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "query_measure_total_hours",
    "select_measure_total_values",
]

_NS_PER_HOUR = 3_600_000_000_000

# The SQL is identical on both backends, so one statement serves each. Every
# selected column is named in the GROUP BY, which keeps it valid under
# MySQL/MariaDB's ONLY_FULL_GROUP_BY.
_SELECT_MEASURE_TOTAL_VALUES = (
    "SELECT m.id, m.tag, m.freq_nhz, m.unit, SUM(bi.num_values) "
    "FROM block_index bi "
    "JOIN measure m ON m.id = bi.measure_id "
    "WHERE m.freq_nhz > 0 "
    "GROUP BY m.id, m.tag, m.freq_nhz, m.unit"
)

# Must match the column order of _SELECT_MEASURE_TOTAL_VALUES.
_MEASURE_TOTAL_HOURS_KEYS = (
    "measure_id",
    "measure_tag",
    "freq_nhz",
    "units",
    "total_num_values",
)


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
            "Measure statistics require direct database access; this SDK "
            "instance is in 'api' mode."
        )

    return sdk.sql_handler.connection(begin=False)


def select_measure_total_values(sdk: "AtriumSDK") -> list[tuple]:
    """Sum the stored sample count per measure across every device.

    Aggregates ``block_index.num_values`` — the number of samples actually
    written during ingestion — so gaps in acquisition are excluded rather than
    counted as coverage. Measures with ``freq_nhz = 0`` are omitted, since a
    sample count cannot be converted to a duration without a frequency.

    Results are returned in whatever order the database produces; callers that
    need a specific order must sort themselves.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :return: List of tuples, one per measure::

            (measure_id, measure_tag, freq_nhz, units, total_num_values)
    """
    with _handler_connection(sdk) as (conn, cursor):
        cursor.execute(_SELECT_MEASURE_TOTAL_VALUES)
        return cursor.fetchall()


def query_measure_total_hours(sdk: "AtriumSDK") -> list[dict]:
    """Return data-coverage hours per measure across all devices.

    Counts stored samples via :func:`select_measure_total_values`, then converts
    to hours using each measure's ``freq_nhz``. Measures with ``freq_nhz = 0``
    (aperiodic / annotation signals) are excluded by the query.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :return: List of dicts, one per measure, in database order — callers that
        need a particular order must sort themselves::

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
    rows = select_measure_total_values(sdk)

    result = []
    for row in rows:
        entry = dict(zip(_MEASURE_TOTAL_HOURS_KEYS, row))
        total_num_values = entry["total_num_values"] or 0
        freq_nhz = entry["freq_nhz"] or 0
        # period_ns = 1e18 / freq_nhz  (freq_nhz = Hz x 1e9, so period_ns = 1e9/Hz = 1e18/freq_nhz)
        total_ns = total_num_values * 1e18 / freq_nhz if freq_nhz > 0 else 0.0
        entry["total_ns"] = total_ns
        entry["total_hours"] = total_ns / _NS_PER_HOUR
        result.append(entry)

    _LOGGER.debug("%d measures queried for hours.", len(result))
    return result
