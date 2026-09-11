from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..attribution_repository import AttributionRepository
from ..attribution_service import AttributionError, AttributionService
from ..attribution_worker import AttributionWorker
from ..models import ManualAttributionRequest, StartAttributionRequest, User


def create_router(
    current_user: Callable[..., User],
    service: AttributionService,
    repository: AttributionRepository,
    worker: AttributionWorker,
    jobs_dir: Path,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/jobs", tags=["speaker-attribution"])

    @router.post("/{job_id}/attribution-runs", status_code=202)
    async def start_run(
        job_id: str,
        payload: StartAttributionRequest,
        user: Annotated[User, Depends(current_user)],
    ) -> dict:
        try:
            run = service.create_run(
                job_id,
                user.id,
                payload.external_processing_consent,
                payload.processing_mode.value,
                payload.profile_id,
                payload.budget_amount,
                payload.budget_currency,
            )
        except AttributionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        worker.notify()
        return _run_response(run, repository)

    @router.get("/{job_id}/attribution-runs/{run_id}")
    async def get_run(
        job_id: str,
        run_id: str,
        user: Annotated[User, Depends(current_user)],
    ) -> dict:
        run = _owned_run(repository, run_id, user.id)
        if run["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="Запуск не найден")
        return _run_response(run, repository)

    @router.post("/{job_id}/attribution-runs/{run_id}/consent/revoke", status_code=204)
    async def revoke_consent(
        job_id: str,
        run_id: str,
        user: Annotated[User, Depends(current_user)],
    ) -> None:
        run = _owned_run(repository, run_id, user.id)
        if run["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="Запуск не найден")
        repository.revoke_consent(run_id, user.id)

    @router.patch("/{job_id}/attribution-runs/{run_id}/segments/{segment_id}")
    async def edit_segment(
        job_id: str,
        run_id: str,
        segment_id: str,
        payload: ManualAttributionRequest,
        user: Annotated[User, Depends(current_user)],
    ) -> dict:
        run = _owned_run(repository, run_id, user.id)
        if run["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="Запуск не найден")
        segments = {item["id"]: item for item in repository.list_segments(run_id)}
        if segment_id not in segments:
            raise HTTPException(status_code=404, detail="Сегмент не найден")
        repository.set_manual_label(segment_id, user.id, payload.speaker_label)
        service.publish_artifacts(run_id)
        return next(item for item in repository.list_segments(run_id) if item["id"] == segment_id)

    @router.get("/{job_id}/attribution-runs/{run_id}/artifacts/{kind}")
    async def artifact(
        job_id: str,
        run_id: str,
        kind: str,
        user: Annotated[User, Depends(current_user)],
    ) -> FileResponse:
        run = _owned_run(repository, run_id, user.id)
        if run["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="Запуск не найден")
        names = {"html": "attributed-transcript.html", "json": "attribution.json"}
        if kind not in names:
            raise HTTPException(status_code=404, detail="Артефакт не найден")
        path = jobs_dir / job_id / "attribution" / run_id / names[kind]
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Артефакт еще не готов")
        return FileResponse(path)

    return router


def _owned_run(repository: AttributionRepository, run_id: str, user_id: str) -> dict:
    try:
        return repository.get_run(run_id, user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Запуск не найден") from exc


def _run_response(run: dict, repository: AttributionRepository) -> dict:
    base = f"/api/v1/jobs/{run['job_id']}/attribution-runs/{run['id']}"
    ready = run["status"] in {"completed", "blocked_budget"}
    return {
        **{key: value for key, value in run.items() if key != "profile_snapshot"},
        "segments": repository.list_segments(run["id"]),
        "attributed_transcript_url": f"{base}/artifacts/html" if ready else None,
        "attribution_json_url": f"{base}/artifacts/json" if ready else None,
    }
