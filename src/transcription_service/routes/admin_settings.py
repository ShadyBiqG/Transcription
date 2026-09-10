from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..admin_service import AdminService
from ..models import AdminSettingsUpdate, User
from ..routerai import RouterAIError


def create_router(require_admin: Callable[..., User], service: AdminService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", tags=["admin-settings"])

    @router.get("/settings")
    async def get_settings(_: Annotated[User, Depends(require_admin)]) -> dict:
        return service.settings_view()

    @router.patch("/settings")
    async def update_settings(
        payload: AdminSettingsUpdate,
        admin: Annotated[User, Depends(require_admin)],
    ) -> dict:
        try:
            return service.update_settings(payload.model_dump(exclude_unset=True), admin.id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/settings/test")
    async def test_settings(admin: Annotated[User, Depends(require_admin)]) -> dict:
        try:
            return {"ok": await service.test_connection(admin.id)}
        except (RouterAIError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/models")
    async def models(_: Annotated[User, Depends(require_admin)]) -> dict:
        return service.list_models()

    @router.post("/models/refresh")
    async def refresh_models(admin: Annotated[User, Depends(require_admin)]) -> dict:
        try:
            return await service.refresh_catalog(admin.id)
        except (RouterAIError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/models/{model_id:path}/endpoints")
    async def model_endpoints(
        model_id: str, _: Annotated[User, Depends(require_admin)]
    ) -> dict:
        try:
            return await service.model_details(model_id)
        except RouterAIError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router
