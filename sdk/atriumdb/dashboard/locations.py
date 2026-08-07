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

"""The location code vocabulary, shared by the request schema and the query layer.

``LOCATION_LOOKUP`` maps the API location codes a dashboard caller sends (e.g.
``"ICU"``) to the exact ``unit.name`` strings stored in the database.

It lives in its own module so that both consumers can reach it without one
importing the other: :mod:`atriumdb.dashboard.schemas` uses it to reject
unknown codes at the request boundary, and
:mod:`atriumdb.dashboard.encounter_queries` uses it to translate accepted codes
into the ``unit.name`` values passed to SQL.
"""

from __future__ import annotations

# Maps API location codes to the exact strings stored in unit.name.
# Filtering is performed against unit.name (not unit.type).
# Populate by running: SELECT DISTINCT name FROM unit;
LOCATION_LOOKUP: dict[str, list[str]] = {
    "ICU": ["ICU"],
    "OR": ["OR"],
}


def resolve_location_codes(codes: list[str]) -> list[str]:
    """Translate API location codes into the ``unit.name`` values to filter on.

    :param codes: API location codes, e.g. ``["ICU", "OR"]``.
    :return: The concatenated ``unit.name`` values the codes map to. Order
        follows the input; duplicates are not removed, as the SQL handler
        applies the list as an ``IN`` clause.
    :raises ValueError: If any code is not present in ``LOCATION_LOOKUP``. The
        request schema applies this same check at the request boundary, so a
        caller coming in over HTTP sees a 422 rather than reaching this point.
    """
    unit_name_list: list[str] = []
    for code in codes:
        if code not in LOCATION_LOOKUP:
            raise ValueError(
                f"Unknown location code {code!r}. "
                f"Valid codes are: {list(LOCATION_LOOKUP)}"
            )
        unit_name_list.extend(LOCATION_LOOKUP[code])
    return unit_name_list
