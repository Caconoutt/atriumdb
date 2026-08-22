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

"""FastAPI router exposing the dashboard's cohort-statistics endpoint.

Takes its SDK from :mod:`atriumdb_dashboard.api.dependencies`, shared with every
other dashboard router, so the package stays self-contained (nothing is borrowed
from the test package) and a single ``app.dependency_overrides`` entry swaps the
SDK for all routers at once.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from atriumdb import AtriumSDK
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.schemas import (
    AggregateStatisticsRequest,
    AggregateStatisticsResponse,
)
from atriumdb_dashboard.statistics_resolver import compute_aggregate_statistics

router = APIRouter()



@router.post("/statistics", response_model=AggregateStatisticsResponse)
async def post_cohort_statistics(
    request: AggregateStatisticsRequest,
    x_request_id: str | None = Header(default=None),
    sdk: AtriumSDK = Depends(get_sdk_instance),
):
    """Compute per-cohort per-patient signal statistics over an observation window.

    Delegates to
    :func:`~atriumdb_dashboard.statistics_resolver.compute_aggregate_statistics`,
    which runs in-process against the direct-DB SDK instance injected by
    ``get_sdk_instance``.

    :param request: Parsed request body: the resolved cohorts, the measure
        identifier, the observation window, and the availability threshold.
    :param x_request_id: ``X-Request-ID`` header, prefixed onto every log and
        exclusion record for this request.
    :param sdk: AtriumSDK instance injected by ``get_sdk_instance``.
    :return: :class:`~atriumdb_dashboard.schemas.AggregateStatisticsResponse`
        with one ``CohortStatistics`` per input cohort.
    :raises HTTPException: 400 if ``X-Request-ID`` is missing or empty; 422 if
        the request names a measure that does not exist in the dataset.
    """
    if not x_request_id:
        raise HTTPException(
            status_code=400,
            detail="X-Request-ID header is required and must be non-empty.",
        )
    try:
        return compute_aggregate_statistics(sdk, request, x_request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
