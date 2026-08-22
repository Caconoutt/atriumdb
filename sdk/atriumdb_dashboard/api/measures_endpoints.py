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

"""FastAPI router exposing the dashboard's measure-statistics endpoints.

Takes its SDK from :mod:`atriumdb_dashboard.api.dependencies`, shared with every
other dashboard router, so the package stays self-contained (nothing is borrowed
from the test package) and a single ``app.dependency_overrides`` entry swaps the
SDK for all routers at once.
"""

from fastapi import APIRouter, Depends

from atriumdb import AtriumSDK
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.queries import query_measure_total_hours

router = APIRouter()



@router.get("/hours")
async def get_measure_total_hours(
        atriumdb_sdk: AtriumSDK = Depends(get_sdk_instance)):
    """Return per-measure data-coverage hours across all devices.

    :param atriumdb_sdk: AtriumSDK instance injected by ``get_sdk_instance``.
    :return: List of per-measure dicts as documented on
        :func:`~atriumdb_dashboard.queries.query_measure_total_hours`.
    """
    return query_measure_total_hours(atriumdb_sdk)
