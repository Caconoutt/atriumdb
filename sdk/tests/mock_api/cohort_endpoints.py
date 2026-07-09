from fastapi import APIRouter, Depends, Header, HTTPException
from atriumdb import AtriumSDK

from atriumdb.dashboard.schemas import AggregateStatisticsRequest, AggregateStatisticsResponse
from tests.mock_api.sdk_dependency import get_sdk_instance

router = APIRouter()


@router.post("/statistics", response_model=AggregateStatisticsResponse)
async def post_cohort_statistics(
    request: AggregateStatisticsRequest,
    x_request_id: str | None = Header(default=None),
    sdk: AtriumSDK = Depends(get_sdk_instance),
):
    if not x_request_id:
        raise HTTPException(
            status_code=400,
            detail="X-Request-ID header is required and must be non-empty.",
        )
    try:
        return sdk.dashboard_compute_statistics(request, x_request_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
