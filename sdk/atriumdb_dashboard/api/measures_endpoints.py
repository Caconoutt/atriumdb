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

Takes the direct-DB SDK from
:func:`~atriumdb_dashboard.api.dependencies.get_meta_sdk`: this endpoint sums
``block_index.num_values`` in raw SQL, which needs ``sdk.sql_handler`` and so
cannot run in api mode.
"""

from fastapi import APIRouter, Depends

from atriumdb import AtriumSDK
from atriumdb_dashboard.api.dependencies import get_meta_sdk
from atriumdb_dashboard.queries import query_measure_total_hours

router = APIRouter()



@router.get("/hours")
def get_measure_total_hours(
        atriumdb_sdk: AtriumSDK = Depends(get_meta_sdk)):
    """Return per-measure data-coverage hours across all devices.


    Declared ``def`` rather than ``async def`` deliberately: every resolver below
    is synchronous and blocking, with nothing awaitable anywhere, so an
    ``async def`` handler would run the whole request on the event loop and stop
    the process serving anything else — ``/health`` included — for its duration.
    A plain ``def`` makes FastAPI run it in a threadpool instead. See
    :data:`~atriumdb_dashboard.pipeline.data_sdk_lock` for what that
    concurrency then requires.

    :param atriumdb_sdk: AtriumSDK instance injected by ``get_meta_sdk``.
    :return: List of per-measure dicts as documented on
        :func:`~atriumdb_dashboard.queries.query_measure_total_hours`.
    """
    return query_measure_total_hours(atriumdb_sdk)
