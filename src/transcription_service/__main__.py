from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный сервис транскрибации noScribe")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания")
    parser.add_argument("--port", type=int, default=8000, help="TCP-порт")
    parser.add_argument("--reload", action="store_true", help="Перезапуск при изменении кода")
    args = parser.parse_args()
    uvicorn.run(
        "transcription_service.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
