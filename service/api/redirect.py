# lineage: REQ-GF-001 / implement.links_api, REQ-BF-002 / implement.analytics,
#          REQ-AMBIGUOUS / implement.enterprise_wiring
"""Public redirect endpoint (registered last so fixed routes win)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse

from service.analytics import ClickEvent
from service.api.deps import get_link_service, limit_redirects
from service.db import utcnow
from service.links import LinkService
from service.observability import get_logger

router = APIRouter(tags=["redirect"])
log = get_logger("redirect")


def record_click(request: Request, link_id: int) -> None:
    """Hand the click to the background recorder; never raises (analytics cannot fail redirects)."""
    try:
        recorder = getattr(request.app.state, "recorder", None)
        if recorder is None:
            return
        recorder.record(
            ClickEvent(
                link_id=link_id,
                occurred_at=utcnow(),
                ip=request.client.host if request.client else "",
                user_agent=request.headers.get("user-agent", ""),
                referrer=request.headers.get("referer"),
            )
        )
    except Exception as exc:
        log.warning("click not recorded", extra={"error": type(exc).__name__})


@router.get(
    "/{code}",
    status_code=302,
    response_class=RedirectResponse,
    responses={
        404: {"description": "Unknown code"},
        410: {"description": "Expired or disabled"},
        429: {"description": "Rate limited"},
    },
    dependencies=[Depends(limit_redirects)],
)
async def follow(
    request: Request,
    code: Annotated[str, Path(min_length=1, max_length=64)],
    service: Annotated[LinkService, Depends(get_link_service)],
) -> RedirectResponse:
    link = await service.resolve(code)
    record_click(request, link.id)
    if service.metrics is not None:
        service.metrics.redirects.labels("ok").inc()
    return RedirectResponse(
        link.target_url,
        status_code=service.settings.redirect_status_code,
        headers={"Cache-Control": "private, no-store"},
    )
