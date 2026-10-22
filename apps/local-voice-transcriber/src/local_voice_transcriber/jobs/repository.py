"""SQLite-хранилище заданий: lease, recovery и неизменяемые ASR source records."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from ..asr.models import AsrRecognition
from .models import Job, JobRun, JobStage, JobStatus


class JobRepositoryError(RuntimeError):
    """Базовая ошибка состояния durable jobs."""


class ImmutableSourceError(JobRepositoryError):
    """Повторная обработка пытается заменить уже опубликованный source result."""


class InvalidRunState(JobRepositoryError):
    """Операция не разрешена в текущем состоянии job run."""


class JobRepository:
    """Независимый SQLite repository для candidate-приложения.

    Каждый короткий метод открывает собственное соединение. Это исключает передачу
    connection между asyncio-задачами и даёт атомарный lease через BEGIN IMMEDIATE.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

    def migrate(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS next_jobs (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'uploaded', 'queued', 'processing', 'ready', 'failed',
                        'cancelled', 'stalled'
                    )),
                    current_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS next_job_runs (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES next_jobs(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    asr_pack_id TEXT NOT NULL,
                    voice_pack_id TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued', 'processing', 'ready', 'failed', 'cancelled', 'stalled'
                    )),
                    stage TEXT CHECK(stage IN (
                        'extracting_audio', 'asr', 'diarizing', 'matching', 'publishing'
                    )),
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1),
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
                    stage_manifest_json TEXT NOT NULL DEFAULT '{}',
                    asr_digest TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(job_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS next_job_runs_claim_idx
                    ON next_job_runs(status, created_at);
                CREATE INDEX IF NOT EXISTS next_job_runs_lease_idx
                    ON next_job_runs(status, lease_expires_at);

                CREATE TABLE IF NOT EXISTS next_asr_segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_run_id TEXT NOT NULL REFERENCES next_job_runs(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
                    raw_text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    confidence REAL,
                    word_timing_status TEXT NOT NULL CHECK(
                        word_timing_status IN ('available', 'unavailable')
                    ),
                    UNIQUE(job_run_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS next_asr_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asr_segment_id INTEGER NOT NULL
                        REFERENCES next_asr_segments(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
                    logprob REAL,
                    word_id INTEGER,
                    UNIQUE(asr_segment_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS next_asr_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asr_segment_id INTEGER NOT NULL
                        REFERENCES next_asr_segments(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms >= start_ms),
                    timing_status TEXT NOT NULL CHECK(timing_status = 'available'),
                    UNIQUE(asr_segment_id, ordinal)
                );
                """
            )

    def create_job(
        self,
        *,
        job_id: str,
        owner_id: str,
        source_name: str,
        source_path: str,
    ) -> Job:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO next_jobs(
                    id, owner_id, source_name, source_path, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, owner_id, source_name, source_path, JobStatus.UPLOADED, now, now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str, owner_id: str | None = None) -> Job:
        with self._connection() as connection:
            if owner_id is None:
                row = connection.execute(
                    "SELECT * FROM next_jobs WHERE id = ?", (job_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM next_jobs WHERE id = ? AND owner_id = ?", (job_id, owner_id)
                ).fetchone()
        if row is None:
            raise KeyError("Задание не найдено")
        return _to_job(row)

    def list_jobs(self, owner_id: str, *, limit: int = 50) -> list[Job]:
        """Возвращает исключительно задания владельца для owner-scoped API."""

        if not 1 <= limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM next_jobs WHERE owner_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
        return [_to_job(row) for row in rows]

    def create_run(
        self,
        *,
        run_id: str,
        job_id: str,
        request_id: str,
        asr_pack_id: str,
        voice_pack_id: str | None = None,
        max_attempts: int = 3,
    ) -> JobRun:
        if max_attempts < 1:
            raise ValueError("max_attempts должен быть положительным")
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM next_job_runs WHERE job_id = ? AND request_id = ?",
                (job_id, request_id),
            ).fetchone()
            if existing is not None:
                return _to_run(existing)
            active = connection.execute(
                "SELECT 1 FROM next_job_runs WHERE job_id = ? AND status IN (?, ?) LIMIT 1",
                (job_id, JobStatus.QUEUED, JobStatus.PROCESSING),
            ).fetchone()
            if active is not None:
                raise InvalidRunState("Запись уже ожидает обработки или обрабатывается")
            if (
                connection.execute("SELECT 1 FROM next_jobs WHERE id = ?", (job_id,)).fetchone()
                is None
            ):
                raise KeyError("Задание не найдено")
            connection.execute(
                """
                INSERT INTO next_job_runs(
                    id, job_id, request_id, asr_pack_id, voice_pack_id, status, max_attempts,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    request_id,
                    asr_pack_id,
                    voice_pack_id,
                    JobStatus.QUEUED,
                    max_attempts,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE next_jobs
                SET status = ?, current_run_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.QUEUED, run_id, now, job_id),
            )
        return self.get_run(run_id)

    def recording_statistics_by_owner(self) -> dict[str, dict[str, object]]:
        """Считает все записи владельцев, не извлекая их приватное содержимое."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT owner_id, status, COUNT(*) AS count FROM next_jobs "
                "GROUP BY owner_id, status"
            ).fetchall()
        counters: dict[str, dict[str, int]] = {}
        for row in rows:
            counts = counters.setdefault(str(row["owner_id"]), dict.fromkeys(JobStatus, 0))
            counts[row["status"]] = int(row["count"])
        return {
            owner_id: {"recordings_total": sum(counts.values()), "by_status": counts}
            for owner_id, counts in counters.items()
        }

    def recording_statistics(self) -> dict[str, object]:
        """Возвращает полные агрегаты записей без ограничения выдачи списка."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM next_jobs GROUP BY status"
            ).fetchall()
        counts = dict.fromkeys(JobStatus, 0)
        for row in rows:
            counts[row["status"]] = int(row["count"])
        return {"recordings_total": sum(counts.values()), "by_status": counts}

    def get_run(self, run_id: str) -> JobRun:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM next_job_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Запуск задания не найден")
        return _to_run(row)

    def get_run_for_job(self, *, run_id: str, job_id: str, owner_id: str) -> JobRun:
        """Owner-scoped lookup; чужой или несогласованный run выглядит как отсутствующий."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT runs.* FROM next_job_runs AS runs
                JOIN next_jobs AS jobs ON jobs.id = runs.job_id
                WHERE runs.id = ? AND runs.job_id = ? AND jobs.owner_id = ?
                """,
                (run_id, job_id, owner_id),
            ).fetchone()
        if row is None:
            raise KeyError("Запуск задания не найден")
        return _to_run(row)

    def list_runs(self, *, job_id: str, owner_id: str) -> list[JobRun]:
        """Owner-scoped run history, включая готовые candidate reruns."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT runs.* FROM next_job_runs AS runs
                JOIN next_jobs AS jobs ON jobs.id = runs.job_id
                WHERE runs.job_id = ? AND jobs.owner_id = ?
                ORDER BY runs.created_at DESC, runs.id DESC
                """,
                (job_id, owner_id),
            ).fetchall()
        return [_to_run(row) for row in rows]

    def claim_next_run(self, *, worker_id: str, lease_seconds: int) -> tuple[Job, JobRun] | None:
        if lease_seconds < 1:
            raise ValueError("Длительность lease должна быть положительной")
        now = _now()
        expires_at = _format_time(now + timedelta(seconds=lease_seconds))
        now_text = _format_time(now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM next_job_runs
                WHERE status = ? AND cancel_requested = 0
                ORDER BY created_at, id
                LIMIT 1
                """,
                (JobStatus.QUEUED,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE next_job_runs
                SET status = ?, stage = ?, attempt = attempt + 1, lease_owner = ?,
                    lease_expires_at = ?, heartbeat_at = ?, started_at = COALESCE(started_at, ?),
                    error_code = NULL, error_message = NULL
                WHERE id = ? AND status = ? AND cancel_requested = 0
                """,
                (
                    JobStatus.PROCESSING,
                    JobStage.EXTRACTING_AUDIO,
                    worker_id,
                    expires_at,
                    now_text,
                    now_text,
                    row["id"],
                    JobStatus.QUEUED,
                ),
            ).rowcount
            if updated != 1:
                return None
            connection.execute(
                "UPDATE next_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (JobStatus.PROCESSING, now_text, row["job_id"]),
            )
            job_row = connection.execute(
                "SELECT * FROM next_jobs WHERE id = ?", (row["job_id"],)
            ).fetchone()
            run_row = connection.execute(
                "SELECT * FROM next_job_runs WHERE id = ?", (row["id"],)
            ).fetchone()
        assert job_row is not None and run_row is not None
        return _to_job(job_row), _to_run(run_row)

    def heartbeat(self, *, run_id: str, worker_id: str, lease_seconds: int) -> bool:
        now = _now()
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE next_job_runs
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (
                    _format_time(now),
                    _format_time(now + timedelta(seconds=lease_seconds)),
                    run_id,
                    JobStatus.PROCESSING,
                    worker_id,
                ),
            ).rowcount
        return updated == 1

    def stage_manifest(
        self, *, run_id: str, worker_id: str, stage: JobStage, manifest: dict
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT stage_manifest_json FROM next_job_runs
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (run_id, JobStatus.PROCESSING, worker_id),
            ).fetchone()
            if row is None:
                raise InvalidRunState("Worker не владеет запуском")
            stages = json.loads(row["stage_manifest_json"]).get("stages", {})
            stages[str(stage)] = manifest
            connection.execute(
                "UPDATE next_job_runs SET stage = ?, stage_manifest_json = ? WHERE id = ?",
                (stage, json.dumps({"stages": stages}, sort_keys=True), run_id),
            )

    def cancel_requested(self, run_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM next_job_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Запуск задания не найден")
        return bool(row["cancel_requested"])

    def request_cancel(self, run_id: str) -> JobRun:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM next_job_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Запуск задания не найден")
            status = JobStatus(row["status"])
            if status in {JobStatus.READY, JobStatus.FAILED, JobStatus.CANCELLED}:
                return _to_run(row)
            if status in {JobStatus.QUEUED, JobStatus.STALLED}:
                next_status = JobStatus.CANCELLED
                completed_at = now
            else:
                next_status = JobStatus.PROCESSING
                completed_at = None
            connection.execute(
                """
                UPDATE next_job_runs
                SET cancel_requested = 1, status = ?, completed_at = COALESCE(completed_at, ?),
                    lease_owner = CASE WHEN ? = 'cancelled' THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? = 'cancelled' THEN NULL ELSE lease_expires_at END
                WHERE id = ?
                """,
                (next_status, completed_at, next_status, next_status, run_id),
            )
            if next_status is JobStatus.CANCELLED:
                connection.execute(
                    "UPDATE next_jobs SET status = ?, updated_at = ? WHERE current_run_id = ?",
                    (JobStatus.CANCELLED, now, run_id),
                )
        return self.get_run(run_id)

    def mark_cancelled(self, *, run_id: str, worker_id: str) -> JobRun:
        return self._finish(
            run_id=run_id,
            worker_id=worker_id,
            status=JobStatus.CANCELLED,
            error_code=None,
            error_message=None,
        )

    def mark_ready(self, *, run_id: str, worker_id: str) -> JobRun:
        return self._finish(
            run_id=run_id,
            worker_id=worker_id,
            status=JobStatus.READY,
            error_code=None,
            error_message=None,
        )

    def mark_failed(self, *, run_id: str, worker_id: str, error_code: str) -> JobRun:
        return self._finish(
            run_id=run_id,
            worker_id=worker_id,
            status=JobStatus.FAILED,
            error_code=error_code,
            error_message="Локальная обработка завершилась ошибкой",
        )

    def _finish(
        self,
        *,
        run_id: str,
        worker_id: str,
        status: JobStatus,
        error_code: str | None,
        error_message: str | None,
    ) -> JobRun:
        now = _utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT job_id FROM next_job_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Запуск задания не найден")
            updated = connection.execute(
                """
                UPDATE next_job_runs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, completed_at = ?,
                    error_code = ?, error_message = ?
                WHERE id = ? AND status = ? AND lease_owner = ?
                """,
                (status, now, error_code, error_message, run_id, JobStatus.PROCESSING, worker_id),
            ).rowcount
            if updated != 1:
                raise InvalidRunState("Запуск нельзя завершить этим worker")
            connection.execute(
                "UPDATE next_jobs SET status = ?, updated_at = ? WHERE current_run_id = ?",
                (status, now, run_id),
            )
        return self.get_run(run_id)

    def mark_expired_as_stalled(self, *, now: datetime | None = None) -> list[str]:
        moment = now or _now()
        now_text = _format_time(moment)
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, job_id FROM next_job_runs
                WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (JobStatus.PROCESSING, now_text),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE next_job_runs
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL, error_code = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        JobStatus.STALLED,
                        "worker_heartbeat_expired",
                        row["id"],
                        JobStatus.PROCESSING,
                    ),
                )
                connection.execute(
                    "UPDATE next_jobs SET status = ?, updated_at = ? WHERE current_run_id = ?",
                    (JobStatus.STALLED, now_text, row["id"]),
                )
        return [str(row["id"]) for row in rows]

    def requeue_stalled(self) -> tuple[list[str], list[str]]:
        """Возвращает (requeued, exhausted), не повышая attempt дважды."""

        now = _utc_now()
        requeued: list[str] = []
        exhausted: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM next_job_runs WHERE status = ? ORDER BY created_at, id",
                (JobStatus.STALLED,),
            ).fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    status = JobStatus.CANCELLED
                    completed_at = now
                    exhausted.append(str(row["id"]))
                elif row["attempt"] < row["max_attempts"]:
                    status = JobStatus.QUEUED
                    completed_at = None
                    requeued.append(str(row["id"]))
                else:
                    status = JobStatus.FAILED
                    completed_at = now
                    exhausted.append(str(row["id"]))
                connection.execute(
                    """
                    UPDATE next_job_runs
                    SET status = ?, completed_at = ?, error_code = CASE WHEN ? = 'failed'
                        THEN 'retry_exhausted' ELSE error_code END
                    WHERE id = ?
                    """,
                    (status, completed_at, status, row["id"]),
                )
                connection.execute(
                    "UPDATE next_jobs SET status = ?, updated_at = ? WHERE current_run_id = ?",
                    (status, now, row["id"]),
                )
        return requeued, exhausted

    def store_asr_recognition(self, *, run_id: str, recognition: AsrRecognition) -> bool:
        """Сохраняет source один раз; одинаковый retry идемпотентен."""

        payload = _recognition_payload(recognition)
        encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        digest = sha256(encoded_payload).hexdigest()
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT asr_digest FROM next_job_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError("Запуск задания не найден")
            previous_digest = run["asr_digest"]
            if previous_digest:
                if previous_digest == digest:
                    return False
                raise ImmutableSourceError("ASR source result уже сохранён и отличается")
            for segment_ordinal, segment in enumerate(recognition.segments):
                cursor = connection.execute(
                    """
                    INSERT INTO next_asr_segments(
                        job_run_id, ordinal, start_ms, end_ms, raw_text, language, confidence,
                        word_timing_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        segment_ordinal,
                        segment.start_ms,
                        segment.end_ms,
                        segment.text,
                        recognition.language,
                        segment.confidence,
                        segment.word_timing_status,
                    ),
                )
                segment_id = cursor.lastrowid
                assert segment_id is not None
                connection.executemany(
                    """
                    INSERT INTO next_asr_tokens(
                        asr_segment_id, ordinal, text, start_ms, end_ms, logprob, word_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            segment_id,
                            ordinal,
                            token.text,
                            token.start_ms,
                            token.end_ms,
                            token.logprob,
                            token.word_id,
                        )
                        for ordinal, token in enumerate(segment.tokens)
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO next_asr_words(
                        asr_segment_id, ordinal, text, start_ms, end_ms, timing_status
                    ) VALUES (?, ?, ?, ?, ?, 'available')
                    """,
                    [
                        (segment_id, ordinal, word.text, word.start_ms, word.end_ms)
                        for ordinal, word in enumerate(segment.words)
                    ],
                )
            connection.execute(
                "UPDATE next_job_runs SET asr_digest = ? WHERE id = ?", (digest, run_id)
            )
        return True

    def source_segments(self, run_id: str) -> list[sqlite3.Row]:
        with self._connection() as connection:
            return connection.execute(
                "SELECT * FROM next_asr_segments WHERE job_run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=str(row["id"]),
        owner_id=str(row["owner_id"]),
        source_name=str(row["source_name"]),
        source_path=str(row["source_path"]),
        status=JobStatus(row["status"]),
        current_run_id=row["current_run_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _to_run(row: sqlite3.Row) -> JobRun:
    return JobRun(
        id=str(row["id"]),
        job_id=str(row["job_id"]),
        request_id=str(row["request_id"]),
        asr_pack_id=str(row["asr_pack_id"]),
        voice_pack_id=row["voice_pack_id"],
        status=JobStatus(row["status"]),
        stage=JobStage(row["stage"]) if row["stage"] else None,
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        cancel_requested=bool(row["cancel_requested"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _recognition_payload(recognition: AsrRecognition) -> dict[str, object]:
    return {
        "language": recognition.language,
        "segments": [
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
                "confidence": segment.confidence,
                "word_timing_status": segment.word_timing_status,
                "tokens": [
                    (token.text, token.start_ms, token.end_ms, token.logprob, token.word_id)
                    for token in segment.tokens
                ],
                "words": [(word.text, word.start_ms, word.end_ms) for word in segment.words],
            }
            for segment in recognition.segments
        ],
    }


def _now() -> datetime:
    return datetime.now(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _utc_now() -> str:
    return _format_time(_now())
