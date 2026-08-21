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

"""Location validation, backed by the ``unit`` table.

Location codes are not a fixed vocabulary baked into this package. Whatever the
caller sends is checked against ``unit.name`` in the database, so a deployment
that adds a unit gets it immediately with no code change, and two deployments
with different unit names both work without forking this file.

The check uses :meth:`~atriumdb.sql_handler.sql_handler.SQLHandler.select_unit`,
which upstream already implements once on the base handler class (it is
concrete, not abstract), so both the SQLite and MariaDB backends are covered
without adding anything to ``atriumdb``.

Because the vocabulary lives in the database, validation cannot happen in the
Pydantic model the way it used to — a field validator has no SDK and no
connection. It happens at resolve time instead, and
:mod:`atriumdb_dashboard.api.cohort_endpoints` turns
:class:`UnknownLocationError` into a 422 so the HTTP contract is unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atriumdb import AtriumSDK

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "UnknownLocationError",
    "location_exists",
    "validate_location_codes",
]


class UnknownLocationError(ValueError):
    """Raised when a requested location has no matching ``unit.name`` row.

    Subclasses :class:`ValueError` so that callers catching ``ValueError``
    continue to work, while the endpoint can catch this specific type and map
    it to a 422 rather than a 500.

    :param unknown: The location codes that were not found.
    """

    def __init__(self, unknown: list[str]):
        self.unknown = list(unknown)
        super().__init__(
            f"Unknown location code(s) {self.unknown}. "
            f"Location must match a unit name recorded in the dataset."
        )


def location_exists(sdk: "AtriumSDK", name: str) -> bool:
    """Report whether ``name`` matches a row in the ``unit`` table.

    Delegates to ``sdk.sql_handler.select_unit``, which returns the matching
    ``(id, institution_id, name, type)`` tuple or ``None``.

    Matching is done by the database, so its collation decides case
    sensitivity: SQLite compares ``TEXT`` case-sensitively, while MariaDB's
    default ``utf8mb4_general_ci`` collation does not. A deployment needing
    identical behaviour across both backends should normalise unit names on
    ingest.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param name: The location string supplied by the caller.
    :return: ``True`` if a unit with that name exists.
    """
    return sdk.sql_handler.select_unit(name=name) is not None


def validate_location_codes(
    sdk: "AtriumSDK",
    codes: list[str] | None,
) -> list[str] | None:
    """Check every requested location against the ``unit`` table.

    Unknown locations are rejected rather than dropped. A location filter
    narrows a cohort, so silently ignoring one would widen the result beyond
    what was asked for and the caller could not tell the difference between
    "nobody was in that unit" and "that unit name was a typo".

    Distinct codes are looked up once each, so a repeated code costs one query,
    not several.

    :param sdk: AtriumSDK instance in direct-DB mode.
    :param codes: Location strings from the request. ``None`` or empty means no
        location filter, and is returned unchanged without any query.
    :return: ``codes`` unchanged when every entry is known.
    :raises UnknownLocationError: If any code has no matching unit.
    """
    if not codes:
        return codes

    checked: dict[str, bool] = {}
    for code in codes:
        if code not in checked:
            checked[code] = location_exists(sdk, code)

    unknown = [code for code in dict.fromkeys(codes) if not checked[code]]
    if unknown:
        _LOGGER.warning(
            "Rejected request naming location(s) with no matching unit: %s",
            unknown,
        )
        raise UnknownLocationError(unknown)

    return codes
