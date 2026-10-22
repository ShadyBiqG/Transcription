"""SQLite-хранилище изолированного фундамента Transcription Next."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    """Возвращает время UTC в однозначном ISO-формате."""

    return datetime.now(UTC).isoformat()


def session_expiry(days: int) -> str:
    """Рассчитывает срок действия новой сессии."""

    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


class Database:
    """Минимальный слой SQLite без хранения секретов в открытом виде."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Создаёт отдельный каталог и схему кандидата."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    login TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'admin')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    csrf_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions(token_hash);
                CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    actor_id TEXT REFERENCES users(id),
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def ping(self) -> bool:
        try:
            with self.connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def create_user(self, *, login: str, password_hash: str, role: str = "user") -> dict[str, Any]:
        user_id = str(uuid4())
        created_at = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO users (id, login, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (user_id, login, password_hash, role, created_at),
            )
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_login(self, login: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, login, role, is_active, created_at FROM users ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def count_active_admins(self) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()
        return int(row["count"])

    def update_user(
        self,
        *,
        user_id: str,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> dict[str, Any] | None:
        user = self.get_user(user_id)
        if user is None:
            return None
        fields: list[str] = []
        values: list[object] = []
        if role is not None:
            fields.append("role = ?")
            values.append(role)
        if is_active is not None:
            fields.append("is_active = ?")
            values.append(int(is_active))
        if not fields:
            return user
        values.append(user_id)
        with self.connection() as connection:
            connection.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        return self.get_user(user_id)

    def create_session(
        self,
        *,
        user_id: str,
        token_hash: str,
        csrf_hash: str,
        expires_at: str,
    ) -> dict[str, Any]:
        session_id = str(uuid4())
        created_at = utc_now()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, token_hash, csrf_hash, expires_at, revoked_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (session_id, user_id, token_hash, csrf_hash, expires_at, created_at),
            )
        return {
            "id": session_id,
            "user_id": user_id,
            "token_hash": token_hash,
            "csrf_hash": csrf_hash,
            "expires_at": expires_at,
        }

    def find_active_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT sessions.id AS session_id, sessions.user_id, sessions.token_hash,
                       sessions.csrf_hash, sessions.expires_at, users.id, users.login,
                       users.role, users.is_active, users.created_at
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > ?
                  AND users.is_active = 1
                """,
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def revoke_session(self, session_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (utc_now(), session_id),
            )

    def add_audit_event(
        self,
        *,
        actor_id: str | None,
        action: str,
        target_type: str,
        target_id: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (id, actor_id, action, target_type, target_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), actor_id, action, target_type, target_id, utc_now()),
            )
