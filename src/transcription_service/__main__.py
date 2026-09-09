from __future__ import annotations

import argparse
import getpass

import uvicorn

from .auth import hash_password
from .config import Settings
from .database import JobRepository
from .models import UserRole


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный сервис транскрибации noScribe")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания")
    parser.add_argument("--port", type=int, default=8000, help="TCP-порт")
    parser.add_argument("--reload", action="store_true", help="Перезапуск при изменении кода")
    parser.add_argument(
        "--create-admin",
        metavar="EMAIL",
        help="Создать администратора или повысить существующего пользователя",
    )
    args = parser.parse_args()
    if args.create_admin:
        settings = Settings.from_env()
        repository = JobRepository(settings.database_path)
        repository.initialize()
        email = args.create_admin.strip().lower()
        existing = repository.get_user_by_email(email)
        if existing:
            repository.set_user_role(existing.id, UserRole.ADMIN)
            print(f"Пользователь {email} теперь администратор")
            return
        password = getpass.getpass("Пароль администратора: ")
        if len(password) < 8:
            parser.error("Пароль должен содержать не менее 8 символов")
        repository.create_user(email, hash_password(password), UserRole.ADMIN)
        print(f"Администратор {email} создан")
        return
    uvicorn.run(
        "transcription_service.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
