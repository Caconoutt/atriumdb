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

from fastapi import APIRouter, Depends, Header

from atriumdb import AtriumSDK
from atriumdb.dashboard.schemas import CohortDefinitionRequest, MrnCohortResponse
from tests.mock_api.sdk_dependency import get_sdk_instance

router = APIRouter()


@router.post("", response_model=MrnCohortResponse)
async def post_cohorts(
    body: CohortDefinitionRequest,
    x_request_id: str = Header(default=""),
    sdk: AtriumSDK = Depends(get_sdk_instance),
) -> MrnCohortResponse:
    """Resolve cohort definitions into validated MRN lists.

    Routes to Priority 1A (MRN validation) or 1B (demographic filter) based
    on ``body.type``. Delegates entirely to
    :meth:`~atriumdb.AtriumSDK.dashboard_resolve_cohort`, which runs the
    resolution in-process against the direct-DB SDK instance injected by
    ``get_sdk_instance``.

    :param body: Parsed request body containing the cohort type, the shared
        ``admissionDateRange``, and one or more cohort definitions.
    :param x_request_id: Optional ``X-Request-ID`` header for log correlation,
        echoed back in the response ``requestId`` field.
    :param sdk: AtriumSDK instance injected by ``get_sdk_instance``.
    :return: :class:`~atriumdb.dashboard.schemas.MrnCohortResponse` with one
        resolved cohort per input cohort.
    """
    return sdk.dashboard_resolve_cohort(body, request_id=x_request_id)
