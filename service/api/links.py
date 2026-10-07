# lineage: REQ-GF-001 / implement.links_api,
#          REQ-AMBIGUOUS / implement.enterprise_wiring, implement.api_key_auth
"""Link management API (/api/v1/links)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status

from service.api.deps import get_link_service, limit_writes, require_api_key
from service.links import LinkService
from service.models import ApiKey
from service.schemas import LinkCreate, LinkOut, LinkUpdate

router = APIRouter(prefix="/api/v1/links", tags=["links"])
Service = Annotated[LinkService, Depends(get_link_service)]
Caller = Annotated[ApiKey | None, Depends(require_api_key)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=LinkOut,
    dependencies=[Depends(limit_writes)],
)
async def create_link(
    payload: LinkCreate,
    response: Response,
    service: Service,
    caller: Caller,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> LinkOut:
    owner = caller.id if caller is not None else None
    link, created = await service.create(payload, idempotency_key, owner_key_id=owner)
    if not created:
        response.headers["Idempotent-Replayed"] = "true"
    response.headers["Location"] = f"/api/v1/links/{link.code}"
    return service.to_out(link)


@router.get("/{code}", response_model=LinkOut)
async def get_link(code: str, service: Service) -> LinkOut:
    return service.to_out(await service.get(code))


@router.patch("/{code}", response_model=LinkOut, dependencies=[Depends(limit_writes)])
async def update_link(code: str, patch: LinkUpdate, service: Service, caller: Caller) -> LinkOut:
    actor = caller.id if caller is not None else None
    return service.to_out(await service.update(code, patch, actor_key_id=actor))


@router.delete(
    "/{code}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(limit_writes)]
)
async def delete_link(code: str, service: Service, caller: Caller) -> Response:
    await service.delete(code, actor_key_id=caller.id if caller is not None else None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
