from __future__ import annotations

import html
import json
import unicodedata
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from .admin_repository import AdminRepository
from .admin_service import AdminService
from .attribution_repository import AttributionRepository
from .database import JobRepository
from .frames import FFmpegFrameExtractor, choose_frame_times
from .models import JobStatus
from .routerai import RouterAIClient, RouterAIError, RouterAIResult
from .transcript_parser import parse_noscribe_html


class AttributionError(RuntimeError):
    pass


class AttributionService:
    def __init__(
        self,
        jobs: JobRepository,
        repository: AttributionRepository,
        admin_repository: AdminRepository,
        admin_service: AdminService,
        routerai: RouterAIClient,
        extractor: FFmpegFrameExtractor,
        jobs_dir: Path,
        max_frames: int = 3,
    ) -> None:
        self.jobs = jobs
        self.repository = repository
        self.admin_repository = admin_repository
        self.admin_service = admin_service
        self.routerai = routerai
        self.extractor = extractor
        self.jobs_dir = jobs_dir
        self.max_frames = max_frames

    def create_run(
        self,
        job_id: str,
        user_id: str,
        consent: bool,
        profile_id: str,
        budget_amount: str | None,
        budget_currency: str,
    ) -> dict[str, Any]:
        job = self.jobs.get(job_id, user_id)
        if job is None:
            raise AttributionError("Задание не найдено")
        if job.status is not JobStatus.COMPLETED:
            raise AttributionError("Транскрипция еще не завершена")
        directory = self.jobs_dir / job.id
        if not (directory / "transcript.html").is_file():
            raise AttributionError("HTML-транскрипт не найден")
        profile = self.admin_service.profile_snapshot(profile_id)
        return self.repository.create_run(
            job_id,
            user_id,
            consent,
            profile,
            budget_amount,
            budget_currency,
        )

    async def process_run(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        try:
            job = self.jobs.get(run["job_id"])
            if job is None:
                raise AttributionError("Исходное задание не найдено")
            job_dir = self.jobs_dir / job.id
            run_dir = job_dir / "attribution" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            segments = self.repository.list_segments(run_id)
            if not segments:
                parsed = parse_noscribe_html(job_dir / "transcript.html")
                segments = self.repository.replace_segments(
                    run_id,
                    [
                        {
                            "id": item.id,
                            "ordinal": item.ordinal,
                            "source_anchor": item.source_anchor,
                            "source_label": item.source_label,
                            "start_ms": item.start_ms,
                            "end_ms": item.end_ms,
                            "text": item.text,
                        }
                        for item in parsed
                    ],
                )

            profile = run["profile_snapshot"]
            key = self.admin_service.get_api_key() if run["consent_at"] else None
            external_enabled = bool(profile.get("provider_enabled") and key)
            for segment in segments:
                if segment["status"] != "pending":
                    continue
                latest_run = self.repository.get_run(run_id)
                if latest_run.get("consent_revoked_at"):
                    external_enabled = False
                if not external_enabled:
                    self.repository.set_attribution(
                        segment["id"], "unknown", None, None, reason="Внешняя обработка отключена"
                    )
                    continue
                if self._budget_blocked(run):
                    self.repository.finish(
                        run_id,
                        "blocked_budget",
                        "budget_exceeded",
                        "Достигнут лимит расходов",
                    )
                    self.publish_artifacts(run_id)
                    return
                frame_paths = self._extract_segment_frames(job_dir, run_dir, run_id, segment)
                try:
                    model_result, call_id = await self._analyze_with_profile(
                        run, segment, frame_paths, key, profile
                    )
                except AttributionError as exc:
                    self.repository.set_attribution(
                        segment["id"],
                        "unknown",
                        None,
                        None,
                        reason=f"Внешняя модель недоступна: {exc}",
                    )
                    continue
                aggregate = aggregate_frame_results(model_result.results)
                self.repository.set_attribution(
                    segment["id"],
                    aggregate["status"],
                    aggregate["speaker_label"],
                    aggregate["confidence"],
                    aggregate["highlight_bbox"],
                    aggregate["label_bbox"],
                    aggregate["reason"],
                    call_id,
                )
            self.repository.finish(run_id, "completed")
            self.publish_artifacts(run_id)
        except Exception as exc:
            self.repository.finish(run_id, "failed", "attribution_failed", str(exc)[:500])

    def _extract_segment_frames(
        self,
        job_dir: Path,
        run_dir: Path,
        run_id: str,
        segment: dict[str, Any],
    ) -> list[Path]:
        existing = self.repository.list_frames(segment["id"])
        if existing:
            return [run_dir / item["relative_path"] for item in existing]
        paths: list[Path] = []
        for index, timestamp in enumerate(
            choose_frame_times(segment["start_ms"], segment["end_ms"], self.max_frames)
        ):
            relative = Path("evidence") / segment["id"] / f"{index:02d}.jpg"
            extracted = self.extractor.extract(
                job_dir / "source.webm", timestamp, run_dir / relative
            )
            self.repository.add_frame(
                run_id,
                segment["id"],
                extracted.requested_ms,
                extracted.actual_ms,
                relative.as_posix(),
                extracted.sha256,
            )
            paths.append(extracted.path)
        return paths

    async def _analyze_with_profile(
        self,
        run: dict[str, Any],
        segment: dict[str, Any],
        frame_paths: list[Path],
        api_key: str,
        profile: dict[str, Any],
    ) -> tuple[RouterAIResult, str]:
        allowed = set(profile.get("allowed_model_ids") or [])
        candidates = [profile.get("primary_model_id"), profile.get("fallback_model_id")]
        candidates = [model for model in candidates if model and (not allowed or model in allowed)]
        if not candidates:
            raise AttributionError("В профиле нет разрешенной модели")
        last_error: Exception | None = None
        for attempt, model in enumerate(dict.fromkeys(candidates), start=1):
            call_id = self.admin_repository.create_call(
                model,
                len(frame_paths),
                run_id=run["id"],
                job_id=run["job_id"],
                user_id=run["user_id"],
                attempt=attempt,
            )
            try:
                result = await self.routerai.analyze_frames(
                    api_key,
                    model,
                    frame_paths,
                    f"{run['id']}:{segment['id']}:{attempt}",
                    max_attempts=1,
                )
                self.admin_repository.finish_call(
                    call_id,
                    status="completed",
                    actual_model=result.model,
                    service_tier=result.service_tier,
                    generation_id=result.generation_id,
                    provider_request_id=result.provider_request_id,
                    **result.usage,
                )
                if result.generation_id:
                    await self._enrich_cost(call_id, api_key, result.generation_id)
                return result, call_id
            except RouterAIError as exc:
                self.admin_repository.finish_call(
                    call_id,
                    status="outcome_unknown" if exc.outcome_unknown else "failed",
                    error_code=str(exc.status_code or "routerai_error"),
                    error_message=str(exc)[:500],
                )
                last_error = exc
                if exc.outcome_unknown:
                    break
        raise AttributionError(str(last_error or "Модель недоступна"))

    async def _enrich_cost(self, call_id: str, api_key: str, generation_id: str) -> None:
        try:
            generation = await self.routerai.fetch_generation(api_key, generation_id)
        except Exception:
            return
        if not generation:
            return
        cost = generation.get("total_cost")
        self.admin_repository.finish_call(
            call_id,
            actual_provider=generation.get("provider"),
            provider_cost=str(cost) if cost is not None else None,
            currency="RUB",
            cost_source="provider" if cost is not None else "unavailable",
        )

    def _budget_blocked(self, run: dict[str, Any]) -> bool:
        global_budget = self.admin_repository.configured_budget()
        if global_budget is not None and self.admin_repository.confirmed_spend() >= global_budget:
            return True
        if run.get("budget_amount"):
            usage = self.admin_repository.external_usage(limit=10000)
            run_spend = sum(
                (
                    Decimal(item["provider_cost"])
                    for item in usage["items"]
                    if item["run_id"] == run["id"] and item["provider_cost"]
                ),
                Decimal("0"),
            )
            return run_spend >= Decimal(run["budget_amount"])
        return False

    def publish_artifacts(self, run_id: str) -> tuple[Path, Path]:
        run = self.repository.get_run(run_id)
        segments = self.repository.list_segments(run_id)
        run_dir = self.jobs_dir / run["job_id"] / "attribution" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "job_id": run["job_id"],
            "status": run["status"],
            "segments": [self._artifact_segment(item) for item in segments],
        }
        json_path = run_dir / "attribution.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        rows = []
        for item in segments:
            label = item.get("manual_label") or item.get("speaker_label") or "unknown"
            rows.append(
                "<tr>"
                f"<td>{_format_time(item['start_ms'])}</td>"
                f"<td>{html.escape(label)}</td>"
                f"<td>{html.escape(item['text'])}</td>"
                "</tr>"
            )
        document = (
            "<!doctype html><html lang='ru'><meta charset='UTF-8'>"
            "<title>Транскрипция с говорящими</title>"
            "<style>body{font:16px system-ui;max-width:1100px;margin:auto;padding:24px}"
            "table{border-collapse:collapse;width:100%}"
            "td,th{padding:8px;border-bottom:1px solid #ccc}"
            "th{text-align:left}</style><body><h1>Транскрипция с говорящими</h1>"
            "<table><thead><tr><th>Время</th><th>Говорящий</th><th>Текст</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></body></html>"
        )
        html_path = run_dir / "attributed-transcript.html"
        html_path.write_text(document, encoding="utf-8")
        return json_path, html_path

    def _artifact_segment(self, segment: dict[str, Any]) -> dict[str, Any]:
        frames = self.repository.list_frames(segment["id"])
        return {
            "segment_id": segment["id"],
            "source_anchor": segment["source_anchor"],
            "source_label": segment["source_label"],
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "status": segment["status"],
            "speaker_label": segment.get("manual_label") or segment.get("speaker_label"),
            "confidence": segment.get("confidence"),
            "evidence": [
                {
                    "frame_id": frame["id"],
                    "timestamp_ms": frame["actual_ms"],
                    "sha256": frame["sha256"],
                    "highlight_bbox": segment.get("highlight_bbox"),
                }
                for frame in frames
            ],
        }


def aggregate_frame_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    detected = [item for item in results if item.get("status") == "detected"]
    normalized = [_normalize_label(str(item.get("speaker_label") or "")) for item in detected]
    normalized = [label for label in normalized if label]
    if not normalized:
        return _unknown("Зеленая рамка или подпись не определена")
    counts = Counter(normalized)
    winner, count = counts.most_common(1)[0]
    required = 1 if len(results) == 1 else 2
    if count < required or (len(counts) > 1 and counts.most_common(2)[1][1] == count):
        return _unknown("Кадры дают противоречивые подписи")
    winner_items = [
        item
        for item in detected
        if _normalize_label(str(item.get("speaker_label") or "")) == winner
    ]
    best = max(winner_items, key=lambda item: float(item.get("confidence") or 0))
    display = " ".join(str(best["speaker_label"]).split())
    return {
        "status": "detected",
        "speaker_label": display,
        "confidence": sum(float(item["confidence"]) for item in winner_items) / len(winner_items),
        "highlight_bbox": best.get("highlight_bbox"),
        "label_bbox": best.get("label_bbox"),
        "reason": best.get("reason") or "Подпись подтверждена несколькими кадрами",
    }


def _normalize_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _unknown(reason: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "speaker_label": None,
        "confidence": None,
        "highlight_bbox": None,
        "label_bbox": None,
        "reason": reason,
    }


def _format_time(milliseconds: int) -> str:
    total_seconds = milliseconds // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
