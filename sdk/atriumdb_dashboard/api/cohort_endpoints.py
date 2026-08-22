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

"""FastAPI router exposing ``POST /cohorts``.

Takes its SDK from :mod:`atriumdb_dashboard.api.dependencies`, shared with every
other dashboard router, so the package stays self-contained (nothing is borrowed
from the test package) and a single ``app.dependency_overrides`` entry swaps the
SDK for all routers at once.

Callers wire the router up with
:func:`~atriumdb_dashboard.api.app.mount_dashboard`.
"""

from fastapi import APIRouter, Depends, Header, HTTPException

from atriumdb import AtriumSDK
from atriumdb_dashboard.api.dependencies import get_sdk_instance
from atriumdb_dashboard.cohort_resolver import resolve_cohort
from atriumdb_dashboard.locations import UnknownLocationError
from atriumdb_dashboard.schemas import CohortDefinitionRequest, MrnCohortResponse

router = APIRouter()



@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(..., min_length=1, pattern=r"\S"),
    sdk: AtriumSDK = Depends(get_sdk_instance),
) -> MrnCohortResponse:
    """Resolve cohort definitions into validated MRN lists.

    Routes to Priority 1A (MRN validation) or 1B (demographic filter) based
    on ``body.type``. Delegates entirely to
    :func:`~atriumdb_dashboard.cohort_resolver.resolve_cohort`, which runs the
    resolution in-process against the direct-DB SDK instance injected by
    ``get_sdk_instance``.

    :param body: Parsed request body containing the cohort type, the shared
        ``admissionDateRange``, and one or more cohort definitions.
    :param x_request_id: Required ``X-Request-ID`` header for log correlation,
        echoed back in the response ``requestId`` field. A missing, empty, or
        all-whitespace header is rejected with a 422 before any query runs —
        the ``\\S`` pattern is what covers the whitespace-only case, which
        ``min_length`` alone lets through.
    :param sdk: AtriumSDK instance injected by ``get_sdk_instance``.
    :return: :class:`~atriumdb_dashboard.schemas.MrnCohortResponse` with one
        resolved cohort per input cohort.
    :raises HTTPException: 422 if a requested location matches no unit in the
        dataset.
    """
    try:
        return resolve_cohort(sdk, body, request_id=x_request_id)
    except UnknownLocationError as exc:
        # Locations are validated against the database, so this check cannot
        # run in the Pydantic model the way the sex and date-range checks do.
        # Translating it here keeps bad input a 422 rather than a 500, matching
        # how every other invalid field behaves.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
