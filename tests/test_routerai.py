import asyncio
import json

import httpx
import pytest

from transcription_service.routerai import RouterAIClient, RouterAIError


def test_routerai_catalog_tolerates_nullable_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "vendor/vision",
                        "name": None,
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                        },
                        "supported_parameters": ["response_format", "structured_outputs"],
                    }
                ]
            },
        )

    client = RouterAIClient("https://router.test/api/v1", transport=httpx.MockTransport(handler))
    models = asyncio.run(client.fetch_models())
    assert models[0]["id"] == "vendor/vision"


def test_routerai_sends_images_without_transcript_text(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "Тестовая реплика" not in serialized
        assert "data:image/jpeg;base64" in serialized
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "model": "vendor/vision",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "results": [
                                        {
                                            "frame_index": 0,
                                            "status": "detected",
                                            "speaker_label": "Гость",
                                            "confidence": 0.9,
                                            "highlight_bbox": [0.1, 0.1, 0.4, 0.4],
                                            "label_bbox": [0.1, 0.3, 0.2, 0.35],
                                            "reason": "green border",
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = RouterAIClient("https://router.test/api/v1", transport=httpx.MockTransport(handler))
    result = asyncio.run(client.analyze_frames("secret", "vendor/vision", [frame], "test"))
    assert result.results[0]["speaker_label"] == "Гость"
    assert result.usage["input_units"] == 10


@pytest.mark.parametrize(
    ("status_code", "expected_calls"),
    [(400, 1), (401, 1), (402, 1), (404, 1), (429, 2), (500, 2), (503, 2)],
)
def test_routerai_retries_only_retryable_statuses(
    tmp_path, status_code: int, expected_calls: int
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, json={"error": {"message": "failed"}})

    client = RouterAIClient("https://router.test/api/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(RouterAIError):
        asyncio.run(
            client.analyze_frames(
                "secret", "vendor/vision", [frame], "test", max_attempts=2
            )
        )
    assert calls == expected_calls


def test_routerai_read_timeout_is_outcome_unknown(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    client = RouterAIClient("https://router.test/api/v1", transport=httpx.MockTransport(handler))
    with pytest.raises(RouterAIError) as caught:
        asyncio.run(client.analyze_frames("secret", "vendor/vision", [frame], "test"))
    assert caught.value.outcome_unknown is True
