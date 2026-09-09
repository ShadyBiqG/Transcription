from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

VISION_SCHEMA: dict[str, Any] = {
    "name": "speaker_attribution",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "frame_index",
                        "status",
                        "speaker_label",
                        "confidence",
                        "highlight_bbox",
                        "label_bbox",
                        "reason",
                    ],
                    "properties": {
                        "frame_index": {"type": "integer", "minimum": 0},
                        "status": {
                            "enum": [
                                "detected",
                                "no_highlight",
                                "label_unreadable",
                                "ambiguous",
                            ]
                        },
                        "speaker_label": {"type": ["string", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "highlight_bbox": {
                            "type": ["array", "null"],
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "label_bbox": {
                            "type": ["array", "null"],
                            "items": {"type": "number", "minimum": 0, "maximum": 1},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
}


class RouterAIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class RouterAIResult:
    results: list[dict[str, Any]]
    model: str
    generation_id: str | None
    provider_request_id: str | None
    usage: dict[str, int | None]
    service_tier: str | None = None


class RouterAIClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self.transport,
        )

    async def fetch_models(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.get("/models")
        response.raise_for_status()
        body = response.json()
        data = body.get("data", []) if isinstance(body, dict) else body
        if not isinstance(data, list):
            raise RouterAIError("RouterAI вернул некорректный каталог моделей")
        return [item for item in data if isinstance(item, dict)]

    async def fetch_model_endpoints(self, model_id: str) -> dict[str, Any]:
        normalized = model_id.lstrip("~")
        if "/" not in normalized:
            raise RouterAIError("Некорректный идентификатор модели")
        author, slug = normalized.split("/", 1)
        async with self._client() as client:
            response = await client.get(f"/models/{author}/{slug}/endpoints")
        response.raise_for_status()
        body = response.json()
        return body.get("data", body)

    async def test_key(self, api_key: str) -> bool:
        async with self._client() as client:
            response = await client.get(
                "/credits", headers={"Authorization": f"Bearer {api_key}"}
            )
        return response.status_code == 200

    async def analyze_frames(
        self,
        api_key: str,
        model: str,
        frame_paths: list[Path],
        session_id: str,
        *,
        max_attempts: int = 3,
    ) -> RouterAIResult:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Для каждого кадра найди участника, выделенного зеленой рамкой, и "
                    "дословно прочитай видимую подпись. Анализируй весь кадр: при демонстрации "
                    "говорящий может быть в панели, без демонстрации — на главном экране. "
                    "Не определяй личность по лицу и не додумывай обрезанную подпись."
                ),
            }
        ]
        for path in frame_paths:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": 500,
            "session_id": session_id,
            "response_format": {"type": "json_schema", "json_schema": VISION_SCHEMA},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        last_error: RouterAIError | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                async with self._client() as client:
                    response = await client.post(
                        "/chat/completions", headers=headers, json=payload
                    )
            except httpx.ReadTimeout as exc:
                raise RouterAIError(
                    "Истекло ожидание ответа RouterAI; результат вызова неизвестен",
                    outcome_unknown=True,
                ) from exc
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = RouterAIError("RouterAI недоступен", retryable=True)
                if attempt < max_attempts:
                    await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                raise last_error from exc

            if response.status_code >= 400:
                retryable = response.status_code in {429, 500, 502, 503}
                message = _safe_error(response)
                last_error = RouterAIError(
                    message,
                    status_code=response.status_code,
                    retryable=retryable,
                )
                if retryable and attempt < max_attempts:
                    await asyncio.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                raise last_error

            body = response.json()
            results = _parse_results(body, len(frame_paths))
            usage = _normalize_usage(body.get("usage") or {})
            generation_id = response.headers.get("X-Generation-Id") or body.get("id")
            return RouterAIResult(
                results=results,
                model=str(body.get("model") or model),
                generation_id=generation_id,
                provider_request_id=body.get("id"),
                usage=usage,
                service_tier=body.get("service_tier"),
            )
        assert last_error is not None
        raise last_error

    async def fetch_generation(self, api_key: str, generation_id: str) -> dict[str, Any] | None:
        async with self._client() as client:
            response = await client.get(
                "/generation",
                params={"id": generation_id},
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        body = response.json()
        return body.get("data", body)


def _safe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"RouterAI вернул HTTP {response.status_code}"
    if isinstance(body, dict):
        error = body.get("error") or body.get("detail")
        if isinstance(error, dict):
            error = error.get("message")
        if isinstance(error, str):
            return error[:500]
    return f"RouterAI вернул HTTP {response.status_code}"


def _normalize_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    input_units = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_units = usage.get("output_tokens", usage.get("completion_tokens"))
    total_units = usage.get("total_tokens")
    return {
        "input_units": int(input_units) if input_units is not None else None,
        "output_units": int(output_units) if output_units is not None else None,
        "total_units": int(total_units) if total_units is not None else None,
    }


def _parse_results(body: dict[str, Any], frame_count: int) -> list[dict[str, Any]]:
    choices = body.get("choices") or []
    if not choices:
        raise RouterAIError("RouterAI не вернул результат")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise RouterAIError("Ответ RouterAI обрезан по длине")
    message = choice.get("message") or {}
    if message.get("refusal"):
        raise RouterAIError("Модель отказалась анализировать кадры")
    raw = message.get("content")
    if not isinstance(raw, str) or not raw.strip():
        raise RouterAIError("RouterAI вернул пустой ответ")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouterAIError("RouterAI вернул невалидный JSON") from exc
    results = parsed.get("results") if isinstance(parsed, dict) else None
    if not isinstance(results, list) or len(results) != frame_count:
        raise RouterAIError("Число результатов не совпадает с числом кадров")
    seen: set[int] = set()
    validated = []
    statuses = {"detected", "no_highlight", "label_unreadable", "ambiguous"}
    for item in results:
        if not isinstance(item, dict):
            raise RouterAIError("Некорректный элемент результата")
        index = item.get("frame_index")
        if not isinstance(index, int) or not 0 <= index < frame_count or index in seen:
            raise RouterAIError("Некорректный индекс кадра")
        seen.add(index)
        status = item.get("status")
        if status not in statuses:
            raise RouterAIError("Некорректный статус распознавания")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise RouterAIError("Некорректная уверенность модели")
        label = item.get("speaker_label")
        if status == "detected" and (not isinstance(label, str) or not label.strip()):
            raise RouterAIError("Для найденного говорящего отсутствует подпись")
        for field in ("highlight_bbox", "label_bbox"):
            bbox = item.get(field)
            if bbox is not None and (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or any(not isinstance(value, (int, float)) or not 0 <= value <= 1 for value in bbox)
            ):
                raise RouterAIError("Координаты находятся вне кадра")
        validated.append(item)
    return sorted(validated, key=lambda item: item["frame_index"])
