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

"""Dashboard extensions for AtriumDB.

An additive layer over the ``atriumdb`` package: it imports from the SDK but
the SDK never imports from it, so ``atriumdb`` can be upgraded or re-merged
from upstream without touching anything here.

The HTTP surface lives in :mod:`atriumdb_dashboard.api` and is deliberately
not imported here, so that using the queries does not require FastAPI.
"""

from atriumdb_dashboard.queries import (
    query_measure_total_hours,
    select_measure_total_values,
)

__all__ = [
    "query_measure_total_hours",
    "select_measure_total_values",
]
