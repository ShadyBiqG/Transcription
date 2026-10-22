"""Пароли, сессии, CSRF и RBAC для Transcription Next."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request

from .config import Settings
from .database import Database, session_expiry

LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
PASSWORD_MINIMUM_LENGTH = 12


class ApiError(Exception):
    """Ошибка API, которую приложение сериализует единообразно."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def normalize_login(login: str) -> str:
    """Проверяет и канонизирует публичный логин."""

    normalized = login.strip().casefold()
    if not LOGIN_PATTERN.fullmatch(normalized):
        raise ApiError(
            422,
            "invalid_login",
            (
                "Логин должен содержать от 3 до 64 латинских букв, цифр, точки, "
                "дефисы или подчёркивания"
            ),
        )
    return normalized


def validate_password(password: str) -> None:
    """Проверяет минимальную политику локального пароля."""

    if len(password) < PASSWORD_MINIMUM_LENGTH:
        raise ApiError(
            422,
            "weak_password",
            f"Пароль должен содержать не менее {PASSWORD_MINIMUM_LENGTH} символов",
        )
    if len(password) > 256:
        raise ApiError(422, "invalid_password", "Пароль слишком длинный")


def hash_password(password: str) -> str:
    """Хеширует пароль scrypt с уникальной криптографической солью."""

    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    encoded_salt = base64.urlsafe_b64encode(salt).decode("ascii")
    encoded_hash = base64.urlsafe_b64encode(derived).decode("ascii")
    return f"scrypt$16384$8$1${encoded_salt}${encoded_hash}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Проверяет scrypt-хеш без раскрытия результата сравнением по времени."""

    try:
        algorithm, n, r, p, encoded_salt, encoded_hash = stored_hash.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(encoded_hash.encode("ascii"))
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError, UnicodeError):
        return False
    return hmac.compare_digest(derived, expected)


def token_hash(token: str) -> str:
    """Возвращает безопасное для SQLite представление секретного токена."""

    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class CurrentSession:
    """Аутентифицированная сессия и безопасная проекция пользователя."""

    session_id: str
    user_id: str
    login: str
    role: str
    csrf_hash: str
    expires_at: str

    @property
    def user(self) -> dict[str, Any]:
        return {"id": self.user_id, "login": self.login, "role": self.role}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_current_session(request: Request) -> CurrentSession:
    """Извлекает активную сессию только из изолированной cookie."""

    settings = get_settings(request)
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise ApiError(401, "authentication_required", "Требуется вход в систему")
    session = get_database(request).find_active_session(token_hash(token))
    if session is None:
        raise ApiError(401, "authentication_required", "Сессия отсутствует или истекла")
    return CurrentSession(
        session_id=session["session_id"],
        user_id=session["user_id"],
        login=session["login"],
        role=session["role"],
        csrf_hash=session["csrf_hash"],
        expires_at=session["expires_at"],
    )


SessionDependency = Annotated[CurrentSession, Depends(get_current_session)]


def require_csrf(request: Request, session: SessionDependency) -> CurrentSession:
    """Требует согласованные cookie/header CSRF-токены, привязанные к сессии."""

    settings = get_settings(request)
    header_token = request.headers.get(settings.csrf_header_name)
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    if not header_token or not cookie_token:
        raise ApiError(403, "csrf_required", "Для изменения данных требуется CSRF-токен")
    if not hmac.compare_digest(header_token, cookie_token):
        raise ApiError(403, "csrf_invalid", "CSRF-токен не подтверждён")
    if not hmac.compare_digest(token_hash(header_token), session.csrf_hash):
        raise ApiError(403, "csrf_invalid", "CSRF-токен не подтверждён")
    return session


CsrfSessionDependency = Annotated[CurrentSession, Depends(require_csrf)]


def require_admin(session: SessionDependency) -> CurrentSession:
    """Проверяет техническую роль, не предоставляя доступ к чужому контенту."""

    if session.role != "admin":
        raise ApiError(403, "admin_required", "Требуется роль администратора")
    return session


AdminDependency = Annotated[CurrentSession, Depends(require_admin)]


def issue_session(database: Database, *, user_id: str, settings: Settings) -> tuple[str, str, str]:
    """Создаёт токены и хранит только их хеши."""

    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = session_expiry(settings.session_ttl_days)
    database.create_session(
        user_id=user_id,
        token_hash=token_hash(session_token),
        csrf_hash=token_hash(csrf_token),
        expires_at=expires_at,
    )
    return session_token, csrf_token, expires_at


def session_cookie_max_age(expires_at: str) -> int:
    """Вычисляет остаток срока cookie без отрицательного значения."""

    remaining = datetime.fromisoformat(expires_at).timestamp() - datetime.now(UTC).timestamp()
    return max(1, int(remaining))
