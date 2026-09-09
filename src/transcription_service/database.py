from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import JobStatus, TranscriptionJob


def utc_now() -> datetime:
    return datetime.now(UTC)


class InvalidStateTransition(RuntimeError):
    pass


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
                    language TEXT NOT NULL,
                    model TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    process_exit_code INTEGER
                )
                """
            )

    def create(self, job: TranscriptionJob) -> None:
        values = job.model_dump(mode="json")
        values["status"] = job.status.value
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",  # noqa: S608
                values,
            )

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row) if row else None

    def list(self, limit: int = 50) -> list[TranscriptionJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_job(row) for row in rows]

    def claim_next(self) -> TranscriptionJob | None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?, updated_at = ?,
                    error_code = NULL, error_message = NULL, process_exit_code = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, row["id"]),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return self._to_job(updated)

    def mark_completed(self, job_id: str, exit_code: int = 0) -> TranscriptionJob:
        return self._finish(job_id, JobStatus.COMPLETED, None, None, exit_code)

    def mark_failed(
        self, job_id: str, error_code: str, error_message: str, exit_code: int | None = None
    ) -> TranscriptionJob:
        return self._finish(job_id, JobStatus.FAILED, error_code, error_message, exit_code)

    def _finish(
        self,
        job_id: str,
        status: JobStatus,
        error_code: str | None,
        error_message: str | None,
        exit_code: int | None,
    ) -> TranscriptionJob:
        now = utc_now().isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, completed_at = ?, updated_at = ?, error_code = ?,
                    error_message = ?, process_exit_code = ?
                WHERE id = ? AND status = 'running'
                """,
                (status.value, now, now, error_code, error_message, exit_code, job_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(
                    f"Задание {job_id} нельзя перевести из текущего состояния в {status.value}"
                )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row)

    def recover_running(self) -> list[TranscriptionJob]:
        with self._connect() as connection:
            ids = [
                row["id"]
                for row in connection.execute("SELECT id FROM jobs WHERE status = 'running'")
            ]
            now = utc_now().isoformat()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', completed_at = ?, updated_at = ?,
                    error_code = 'service_restarted',
                    error_message = 'Обработка прервана перезапуском сервиса'
                WHERE status = 'running'
                """,
                (now, now),
            )
        return [job for job_id in ids if (job := self.get(job_id)) is not None]

    @staticmethod
    def _to_job(row: sqlite3.Row) -> TranscriptionJob:
        return TranscriptionJob.model_validate(dict(row))
