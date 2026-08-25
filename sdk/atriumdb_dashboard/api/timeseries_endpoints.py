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

"""FastAPI router exposing the dashboard's cohort time-series endpoint.

Takes its SDK from :mod:`atriumdb_dashboard.api.dependencies`, shared with every
other dashboard router, so the package stays self-contained (nothing is borrowed
from the test package) and a single ``app.dependency_overrides`` entry swaps the
SDK for all routers at once.

Deliberately a separate endpoint from ``POST /cohorts/statistics`` rather than a
``vizType`` branch on it: this one requires ``interval_ns`` (meaningless for a
box or violin plot), evaluates availability per interval rather than once over
the window, and returns a differently shaped response. Folding the two together
would mean a conditionally-required request field and a union response, which is
markedly worse to consume and to generate clients for. What the two genuinely
share is internal, and lives in :mod:`atriumdb_dashboard.pipeline`.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from atriumdb import AtriumSDK
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.schemas import TimeSeriesRequest, TimeSeriesResponse
from atriumdb_dashboard.timeseries_resolver import compute_cohort_timeseries

router = APIRouter()


@router.post("/timeseries", response_model=TimeSeriesResponse)
async def post_cohort_timeseries(
    request: TimeSeriesRequest,
    x_request_id: str | None = Header(default=None),
    sdk: AtriumSDK = Depends(get_sdk_instance),
):
    """Compute per-cohort per-interval per-patient signal means over a window.

    Delegates to
    :func:`~atriumdb_dashboard.timeseries_resolver.compute_cohort_timeseries`,
    which runs in-process against the direct-DB SDK instance injected by
    ``get_sdk_instance``.

    Header handling mirrors ``post_cohort_statistics`` — checked in the body for
    a 400 rather than declared into the signature for a 422 — because this is
    that endpoint's sibling and the dashboard already handles that contract for
    ``/cohorts/statistics``.

    :param request: Parsed request body: the resolved cohorts, the measure
        identifier, the observation window, the interval width, and the
        availability threshold. Pydantic has already rejected a window the
        interval does not divide evenly, a bucket count above ``MAX_INTERVALS``,
        and an ``"all_time"`` window, each as a 422.
    :param x_request_id: ``X-Request-ID`` header, prefixed onto every log and
        exclusion record for this request.
    :param sdk: AtriumSDK instance injected by ``get_sdk_instance``.
    :return: :class:`~atriumdb_dashboard.schemas.TimeSeriesResponse` with one
        ``CohortTimeSeries`` per input cohort.
    :raises HTTPException: 400 if ``X-Request-ID`` is missing or empty; 422 if
        the request names a measure that does not exist in the dataset or one
        that is aperiodic and so has no sampling grid to bucket against.
    """
    if not x_request_id:
        raise HTTPException(
            status_code=400,
            detail="X-Request-ID header is required and must be non-empty.",
        )
    try:
        return compute_cohort_timeseries(sdk, request, x_request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
