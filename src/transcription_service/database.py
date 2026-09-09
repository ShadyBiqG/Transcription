from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .auth import hash_session_token
from .migrations import apply_migrations
from .models import JobStatus, TranscriptionJob, User, UserRole


def utc_now() -> datetime:
    return datetime.now(UTC)


class InvalidStateTransition(RuntimeError):
    pass


class JobRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    original_filename TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
                    language TEXT NOT NULL,
                    model TEXT NOT NULL,
                    speaker_detection TEXT NOT NULL DEFAULT 'auto',
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
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "speaker_detection" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN speaker_detection TEXT NOT NULL DEFAULT 'none'"
                )
            if "user_id" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user','admin'))
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_created "
                "ON jobs(user_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)"
            )
            apply_migrations(connection)

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

    def get(self, job_id: str, user_id: str | None = None) -> TranscriptionJob | None:
        with self._connect() as connection:
            if user_id is None:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
                ).fetchone()
        return self._to_job(row) if row else None

    def list(self, limit: int = 50, user_id: str | None = None) -> list[TranscriptionJob]:
        with self._connect() as connection:
            if user_id is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
        return [self._to_job(row) for row in rows]

    def create_user(
        self, email: str, password_hash: str, role: UserRole = UserRole.USER
    ) -> User:
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            password_hash=password_hash,
            created_at=utc_now(),
            role=role,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO users (id, email, password_hash, created_at, is_active, role) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (
                    user.id,
                    user.email,
                    user.password_hash,
                    user.created_at.isoformat(),
                    user.role.value,
                ),
            )
        return user

    def set_user_role(self, user_id: str, role: UserRole) -> User:
        with self._connect() as connection:
            if role is not UserRole.ADMIN:
                row = connection.execute(
                    "SELECT role, is_active FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if row and row["role"] == UserRole.ADMIN.value and row["is_active"]:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
                    ).fetchone()[0]
                    if count <= 1:
                        raise ValueError("Нельзя понизить последнего активного администратора")
            cursor = connection.execute(
                "UPDATE users SET role = ? WHERE id = ?", (role.value, user_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("Пользователь не найден")
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._to_user(row)

    def list_users(self) -> list[User]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._to_user(row) for row in rows]

    def get_user_by_email(self, email: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
        return self._to_user(row) if row else None

    def create_session(self, user_id: str, token: str, ttl_days: int) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    hash_session_token(token),
                    user_id,
                    now.isoformat(),
                    (now + timedelta(days=ttl_days)).isoformat(),
                ),
            )

    def get_user_by_session(self, token: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ? AND sessions.expires_at > ?
                  AND users.is_active = 1
                """,
                (hash_session_token(token), utc_now().isoformat()),
            ).fetchone()
        return self._to_user(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (hash_session_token(token),)
            )

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

    @staticmethod
    def _to_user(row: sqlite3.Row) -> User:
        values = dict(row)
        values["is_active"] = bool(values["is_active"])
        values.setdefault("role", UserRole.USER.value)
        return User.model_validate(values)
