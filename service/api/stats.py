# lineage: REQ-BF-002 / implement.analytics (see runs/ for the decision trail)
"""Click statistics for a link."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from service.analytics import link_stats
from service.api.deps import get_link_service
from service.links import LinkService

router = APIRouter(prefix="/api/v1/links", tags=["analytics"])


class DayStats(BaseModel):
    day: str
    clicks: int
    unique_visitors: int


class Referrer(BaseModel):
    host: str
    clicks: int


class UserAgentFamily(BaseModel):
    family: str
    clicks: int


class LinkStats(BaseModel):
    code: str
    window_days: int
    total_clicks: int
    unique_visitors: int
    clicks_by_day: list[DayStats]
    top_referrers: list[Referrer]
    top_user_agents: list[UserAgentFamily]


@router.get("/{code}/stats", response_model=LinkStats, summary="Click statistics")
async def stats(
    code: str,
    service: Annotated[LinkService, Depends(get_link_service)],
    days: Annotated[int, Query(ge=1, le=365, description="Window size in days")] = 30,
) -> LinkStats:
    """Unique visitors are counted per day (daily-salted hashes) and summed over the window."""
    link = await service.get(code)
    data = await link_stats(service.session, link.id, days=days)
    return LinkStats(code=link.code, **data)
