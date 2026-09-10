from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..admin_service import AdminService
from ..models import User


def create_router(
    require_admin: Callable[..., User],
    service: AdminService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-statistics"])

    @router.get("/statistics/overview")
    async def overview(_: Annotated[User, Depends(require_admin)]) -> dict:
        return service.overview()

    @router.get("/external-usage")
    async def external_usage(
        _: Annotated[User, Depends(require_admin)],
        date_from: str | None = None,
        date_to: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        return service.external_usage(
            date_from=date_from,
            date_to=date_to,
            user_id=user_id,
            model=model,
            status=status,
            limit=limit,
        )

    return router
