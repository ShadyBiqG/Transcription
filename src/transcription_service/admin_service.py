from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .admin_repository import AdminRepository
from .routerai import RouterAIClient
from .secrets import mask_secret, protect_secret, unprotect_secret

DEFAULT_PRIMARY_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_FALLBACK_MODEL = "qwen/qwen3.8-27b"


class AdminService:
    def __init__(
        self,
        repository: AdminRepository,
        routerai: RouterAIClient,
        jobs_dir: Path,
    ) -> None:
        self.repository = repository
        self.routerai = routerai
        self.jobs_dir = jobs_dir

    def settings_view(self) -> dict[str, Any]:
        values = {
            "provider_enabled": False,
            "primary_model_id": DEFAULT_PRIMARY_MODEL,
            "fallback_model_id": DEFAULT_FALLBACK_MODEL,
            "allowed_model_ids": [],
            "global_budget": None,
            "default_job_budget": None,
            "currency": "RUB",
            **self.repository.get_settings(),
        }
        credential = self.repository.get_credential("routerai")
        values["credential"] = (
            {
                "configured": True,
                "key_hint": credential["key_hint"],
                "enabled": credential["enabled"],
                "last_checked_at": credential["last_checked_at"],
                "last_check_ok": credential["last_check_ok"],
            }
            if credential
            else {"configured": False, "key_hint": None, "enabled": False}
        )
        return values

    def update_settings(self, payload: dict[str, Any], admin_user_id: str) -> dict[str, Any]:
        api_key = payload.pop("api_key", None)
        if api_key:
            stripped = api_key.strip()
            self.repository.save_credential(
                "routerai",
                protect_secret(stripped),
                mask_secret(stripped),
                admin_user_id,
            )
        allowed = payload.get("allowed_model_ids")
        if allowed is not None:
            available = {item["model_id"] for item in self.list_models()["models"]}
            unknown = set(allowed) - available
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"Несовместимые или неизвестные модели: {names}")
            payload["allowed_model_ids"] = list(dict.fromkeys(allowed))
        for key in ("primary_model_id", "fallback_model_id"):
            model = payload.get(key)
            if model and allowed is not None and model not in payload["allowed_model_ids"]:
                raise ValueError(f"{key} должна входить в разрешенный список")
        clean = dict(payload)
        if clean:
            self.repository.update_settings(clean, admin_user_id)
        return self.settings_view()

    async def refresh_catalog(self, admin_user_id: str) -> dict[str, Any]:
        try:
            models = await self.routerai.fetch_models()
        except Exception as exc:
            self.repository.mark_catalog_failure(str(exc))
            raise
        result = self.repository.replace_catalog(models)
        self.repository.audit(
            admin_user_id,
            "catalog.refresh",
            "model_catalog",
            result["id"],
            None,
            {"count": result["count"], "fetched_at": result["fetched_at"]},
        )
        return result

    def list_models(self) -> dict[str, Any]:
        catalog = self.repository.list_models(compatible_only=True)
        allowed = set(self.repository.get_settings().get("allowed_model_ids", []))
        for model in catalog["models"]:
            model["allowed"] = model["model_id"] in allowed
        return catalog

    async def model_details(self, model_id: str) -> dict[str, Any]:
        return await self.routerai.fetch_model_endpoints(model_id)

    def get_api_key(self) -> str | None:
        credential = self.repository.get_credential("routerai")
        if not credential or not credential["enabled"]:
            return None
        return unprotect_secret(credential["ciphertext"])

    async def test_connection(self, admin_user_id: str | None = None) -> bool:
        key = self.get_api_key()
        if key is None:
            return False
        ok = await self.routerai.test_key(key)
        self.repository.mark_credential_check("routerai", ok)
        if not ok:
            return False
        settings = self.settings_view()
        model = settings.get("primary_model_id")
        allowed = settings.get("allowed_model_ids") or []
        if not model or model not in allowed:
            raise ValueError("Выберите и разрешите основную модель")
        test_dir = self.jobs_dir / ".admin"
        test_dir.mkdir(parents=True, exist_ok=True)
        frame = test_dir / "routerai-test.jpg"
        if not frame.exists():
            frame.write_bytes(base64.b64decode(_TEST_JPEG_BASE64))
        call_id = self.repository.create_call(
            model, 1, user_id=admin_user_id
        )
        try:
            result = await self.routerai.analyze_frames(
                key, model, [frame], f"admin-test:{call_id}", max_attempts=1
            )
        except Exception as exc:
            self.repository.finish_call(
                call_id, status="failed", error_message=str(exc)[:500]
            )
            raise
        self.repository.finish_call(
            call_id,
            status="completed",
            actual_model=result.model,
            generation_id=result.generation_id,
            provider_request_id=result.provider_request_id,
            **result.usage,
        )
        if result.generation_id:
            generation = await self.routerai.fetch_generation(key, result.generation_id)
            if generation and generation.get("total_cost") is not None:
                self.repository.finish_call(
                    call_id,
                    actual_provider=generation.get("provider"),
                    provider_cost=str(generation["total_cost"]),
                    cost_source="provider",
                )
        return True

    def profile_snapshot(self, profile_id: str) -> dict[str, Any]:
        settings = self.settings_view()
        return {
            "profile_id": profile_id,
            "provider": "routerai",
            "primary_model_id": settings["primary_model_id"],
            "fallback_model_id": settings["fallback_model_id"],
            "allowed_model_ids": settings["allowed_model_ids"],
            "provider_enabled": settings["provider_enabled"],
        }

    def overview(self) -> dict[str, Any]:
        result = self.repository.overview()
        result["storage_bytes"] = sum(
            path.stat().st_size
            for path in self.jobs_dir.rglob("*")
            if path.is_file()
        ) if self.jobs_dir.exists() else 0
        return result

    def external_usage(self, **filters: Any) -> dict[str, Any]:
        return self.repository.external_usage(**filters)


_TEST_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/"
    "8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/a"
    "AAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EB//xAAUEQEAAAAAAAAAAAAA"
    "AAAAAAAA/9oACAECAQE/EB//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EB//2Q=="
)
