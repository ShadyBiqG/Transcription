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
                        "speaker_label": {"type": ["string", "null"], "maxLength": 80},
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
                        "reason": {"type": "string", "maxLength": 120},
                    },
                },
            }
        },
    },
}

SPEAKER_ATTRIBUTION_PROMPT = """\
Определи подпись активного говорящего отдельно на каждом кадре.
Все кадры в одном запросе выбраны для одной голосовой метки. Сравнивай их,
чтобы лучше различить постоянное выделение демонстрирующего от яркого контура
активного говорящего и прочитать мелкую подпись. Не выдумывай подпись, которой не видно.

Алгоритм для каждого кадра:
1. Осмотри ВЕСЬ кадр. Найди плитку участника (видео или аватар), которую интерфейс
   конференции выделяет зелёной рамкой или зелёным контуром.
2. При демонстрации экрана плитка может находиться в узкой верхней или боковой панели.
   Без демонстрации она может быть крупной плиткой или частью галереи.
3. Не считай выделением зелёные элементы внутри демонстрируемого приложения, текста,
   презентации, панели задач и кнопок интерфейса. Рамка должна относиться к плитке
   участника.
   Если зелёных контуров несколько, сравни кадры: тонкая постоянная рамка может
   обозначать демонстрирующего, а более яркий изменяющийся контур — говорящего. Если это
   нельзя различить надёжно, верни ambiguous.
4. Прочитай подпись, визуально принадлежащую именно выделенной плитке. Верни её
   дословно, сохранив кириллицу, регистр и роль вроде «Гость». Не определяй человека
   по лицу, голосу или другим кадрам и не дополняй обрезанную подпись.

Статусы:
- detected — выделена ровно одна плитка и её подпись читается;
- no_highlight — надёжной зелёной рамки плитки нет;
- label_unreadable — плитка найдена, но подпись нельзя прочитать дословно;
- ambiguous — подходят несколько плиток или непонятно, к какой плитке относится рамка.

speaker_label заполняй только для detected, иначе null. Координаты bbox указывай как
[left, top, right, bottom] в долях размера полного кадра от 0 до 1; если область нельзя
надёжно указать — null. reason — не более восьми слов, только проверяемая причина.
Верни ровно по одному результату для каждого кадра, сохрани номера и порядок кадров.
Формат ответа: JSON-объект {"results": [объекты результатов]}. Каждый объект обязан
содержать frame_index, status, speaker_label, confidence, highlight_bbox, label_bbox
и reason. Не добавляй Markdown и текст за пределами JSON.
"""


class RouterAIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        outcome_unknown: bool = False,
        actual_model: str | None = None,
        generation_id: str | None = None,
        provider_request_id: str | None = None,
        usage: dict[str, int | str | None] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.actual_model = actual_model
        self.generation_id = generation_id
        self.provider_request_id = provider_request_id
        self.usage = usage or {}


@dataclass(frozen=True, slots=True)
class RouterAIResult:
    results: list[dict[str, Any]]
    model: str
    generation_id: str | None
    provider_request_id: str | None
    usage: dict[str, int | str | None]
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
                    f"{SPEAKER_ATTRIBUTION_PROMPT}\n"
                    f"Количество кадров в запросе: {len(frame_paths)}."
                ),
            }
        ]
        for index, path in enumerate(frame_paths):
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"Кадр {index}:"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                }
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": min(1600, 600 + 250 * len(frame_paths)),
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
                    error_text = _safe_error(response).lower()
                    if response.status_code == 400 and any(
                        marker in error_text
                        for marker in (
                            "response_format",
                            "json_schema",
                            "json schema",
                            "structured output",
                            "structured_outputs",
                        )
                    ):
                        compatibility_payload = {
                            **payload,
                            "response_format": {"type": "json_object"},
                        }
                        response = await client.post(
                            "/chat/completions",
                            headers=headers,
                            json=compatibility_payload,
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
            usage = _normalize_usage(body.get("usage") or {})
            generation_id = response.headers.get("X-Generation-Id") or body.get("id")
            provider_request_id = body.get("id")
            actual_model = str(body.get("model") or model)
            try:
                results = _parse_results(body, len(frame_paths))
            except RouterAIError as exc:
                exc.actual_model = actual_model
                exc.generation_id = generation_id
                exc.provider_request_id = provider_request_id
                exc.usage = usage
                raise
            return RouterAIResult(
                results=results,
                model=actual_model,
                generation_id=generation_id,
                provider_request_id=provider_request_id,
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
            if response.status_code in {404, 405}:
                response = await client.get(
                    f"/history/generations/{generation_id}",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        if response.status_code in {404, 405}:
            return None
        response.raise_for_status()
        body = response.json()
        generation = body.get("data", body)
        if not isinstance(generation, dict):
            return None
        if generation.get("total_cost") is None:
            generation["total_cost"] = generation.get("clientCost", generation.get("cost"))
        if generation.get("provider") is None:
            generation["provider"] = generation.get("finalEndpointSlug")
        return generation


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


def _normalize_usage(usage: dict[str, Any]) -> dict[str, int | str | None]:
    input_units = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_units = usage.get("output_tokens", usage.get("completion_tokens"))
    total_units = usage.get("total_tokens")
    normalized: dict[str, int | str | None] = {
        "input_units": int(input_units) if input_units is not None else None,
        "output_units": int(output_units) if output_units is not None else None,
        "total_units": int(total_units) if total_units is not None else None,
    }
    cost_rub = usage.get("cost_rub")
    if cost_rub is not None:
        normalized.update(
            provider_cost=str(cost_rub),
            currency="RUB",
            cost_source="provider_response",
        )
    return normalized


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
