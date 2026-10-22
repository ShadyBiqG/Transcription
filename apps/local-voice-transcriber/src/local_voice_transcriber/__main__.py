"""CLI для запуска кандидата и начального администратора."""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3

import uvicorn

from .auth import hash_password, normalize_login, validate_password
from .config import Settings
from .database import Database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcription Next")
    parser.add_argument("--host", help="Адрес прослушивания кандидата")
    parser.add_argument("--port", type=int, help="Порт прослушивания кандидата")
    parser.add_argument("--create-admin", metavar="LOGIN", help="Создать локального администратора")
    return parser


def _create_admin(settings: Settings, login: str, password: str | None) -> int:
    if password is None:
        password = os.environ.pop("NEXT_ADMIN_PASSWORD_INPUT", None)
    if password is None:
        password = getpass.getpass("Пароль администратора: ")
    login_value = normalize_login(login)
    validate_password(password)
    database = Database(settings.database_path)
    database.initialize()
    try:
        user = database.create_user(
            login=login_value, password_hash=hash_password(password), role="admin"
        )
    except sqlite3.IntegrityError:
        print("Логин уже занят.")
        return 1
    database.add_audit_event(
        actor_id=user["id"],
        action="bootstrap_admin_created",
        target_type="user",
        target_id=user["id"],
    )
    print(f"Создан администратор {user['login']}.")
    return 0


def main() -> int:
    """Разбирает CLI и не запускает legacy-приложение."""

    args = _parser().parse_args()
    settings = Settings.from_env()
    if args.create_admin:
        return _create_admin(settings, args.create_admin, None)
    uvicorn.run(
        "local_voice_transcriber.api:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
