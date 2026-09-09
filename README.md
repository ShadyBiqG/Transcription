# Локальный сервис транскрибации

Первая часть помощника для записей телемостов: веб-сервис принимает `.webm` в локальной сети, последовательно запускает установленный [noScribe](https://noscribe.de/en/) и отдает VTT с таймкодами.

## Используемое ПО

Для локальной транскрибации сервис использует открытое приложение [noScribe](https://github.com/kaixxx/noScribe). noScribe не входит в этот репозиторий и должна быть отдельно установлена на сервере вместе с необходимыми моделями. Наш сервис запускает ее CLI и предоставляет сетевой интерфейс, очередь заданий и хранение результатов.

## Быстрый запуск

```powershell
uv sync --dev
Copy-Item .env.example .env
uv run transcription-service --host 127.0.0.1 --port 8000
```

Откройте `http://127.0.0.1:8000`.

## Запуск в локальной сети

Задайте длинный случайный токен и слушайте все интерфейсы:

```powershell
$env:TRANSCRIPTION_API_TOKEN = '<длинный-случайный-токен>'
uv run transcription-service --host 0.0.0.0 --port 8000
```

Разрешайте порт в Windows Firewall только для профиля `Private`. На другом устройстве откройте `http://<IP-компьютера>:8000` и укажите тот же токен в интерфейсе.

## Конфигурация

Переменные перечислены в `.env.example`. По умолчанию используется:

- `C:\Program Files (x86)\noScribe\noScribe.exe`;
- язык `ru`;
- модель `precise`;
- один последовательный worker;
- локальный каталог `./data`.

Сервис вызывает noScribe напрямую, без shell, с `--no-gui`, `--timestamps` и VTT-выходом. Встроенное определение говорящих отключено: это отдельная часть проекта.

## Проверка

```powershell
uv run pytest
uv run ruff check .
```

Проверить установленный noScribe:

```powershell
& 'C:\Program Files (x86)\noScribe\noScribe.exe' --help
& 'C:\Program Files (x86)\noScribe\noScribe.exe' --help-models
```

Автотесты используют fake runner и не отправляют запись в реальные модели. Для полного smoke-теста загрузите короткий несекретный `.webm` через интерфейс.

## Данные и диагностика

- SQLite: `data/jobs.sqlite3`.
- Артефакты: `data/jobs/<job-id>/`.
- Журнал noScribe: `noscribe.log` внутри каталога задания; он может содержать чувствительный текст и не отдается API.
- Если health показывает `noscribe: unavailable`, проверьте путь и вывод `--help-models`.

Архитектура всех четырех частей описана в [docs/architecture.md](docs/architecture.md).

Полная инструкция по установке как Windows-службы, настройке брандмауэра и загрузке файлов с другого компьютера находится в [docs/windows-server-deployment.md](docs/windows-server-deployment.md).
