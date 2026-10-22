# Установка на Windows Server

Запустите install-windows.ps1 из этого каталога. Пример:

    powershell -ExecutionPolicy Bypass -File .\deploy\install-windows.ps1 -RepositoryPath . -DataPath E:\TranscriptionData

## Первая установка из Git через BAT

Когда на сервере ещё нет рабочей копии, достаточно заранее скопировать на него
единственный [install-from-git.bat](install-from-git.bat), а затем выполнить в
`cmd.exe`:

```bat
install-from-git.bat "<URL Git-репозитория>" "C:\Services\TranscriptionNext" "C:\ProgramData\TranscriptionNext" "<опубликованная-ветка-кандидата>"
```

При запуске из PowerShell указывайте команду первой. Самый надёжный вариант —
одна строка без символов переноса:

```powershell
& "C:\Users\AI\install-from-git-07e57ab.bat" "https://github.com/ShadyBiqG/Transcription.git" "C:\Services\TranscriptionNext" "E:\TranscriptionData" "codex/transcription-next" --update
```

Сам BAT намеренно содержит только ASCII-символы: `cmd.exe` может разобрать
файл раньше, чем применит смену кодовой страницы, и тогда кириллица в нём
приводит к ложным командам.

Первые четыре аргумента обязательны: URL, новый каталог для clone, отдельный
data root и branch. Не указывайте каталог legacy-приложения ни как clone, ни
как data root. После опубликования новой версии повторите ту же команду с
`--update`; перед `git pull --ff-only` BAT проверит origin, ветку и чистоту
рабочей директории. Любое расхождение прекращает установку без перезаписи
файлов.

Ключ UpdateRepository выполняет только безопасный git pull --ff-only. Ключ
OpenFirewall открывает candidate port в профиле Private.

Скрипт создаёт отдельный data root, локальное окружение через uv и файл
candidate.env.ps1 вне репозитория. Затем он автоматически скачивает в этот
data root закреплённые ASR, VAD, диаризационную и голосовую модели, а также
FFmpeg. Каждый файл сверяется по SHA-256, а до создания администратора
установщик действительно открывает ASR/VAD, Sherpa-диаризацию и embedding
extractor локально. Это первая загрузка примерно 1 ГБ; на диске с data root
должно быть не менее 5 ГБ свободного места. Пока нет активного администратора,
скрипт запрашивает его логин (в том числе адрес e-mail) и пароль, создаёт его
через CLI приложения и не сохраняет пароль в файле конфигурации. Поэтому после
прерванной первоначальной установки достаточно повторно запустить скрипт: он
снова предложит создать администратора. Для unattended-установки передайте
BootstrapAdminLogin и BootstrapAdminPassword (SecureString).

Перед загрузкой моделей установщик проверяет импорт ONNX Runtime. Если Windows
не содержит нужный Microsoft Visual C++ Runtime, он скачивает официальный
`vc_redist.x64.exe`, проверяет подпись Microsoft и запускает его с параметрами
`/install /quiet /norestart`. Для этого потребуется подтверждение UAC. Если
установщик Microsoft потребует перезагрузку, перезагрузите сервер и повторите
ту же команду установки.

Назначьте отдельную service account без ACL на legacy data. Установщик выдаёт
ей Modify только на отдельный `DataPath`, чтобы SQLite мог создать журнал и
миграции при запуске. По умолчанию service account — текущая учётная запись,
выполняющая установку. Поэтому для запуска launcher из `C:\Users\AI` установку
тоже выполняйте от этой же учётной записи; UAC-повышение не меняет её имя. Для
другой учётной записи передайте
`-ServiceAccount "DOMAIN\service-user"` при прямом запуске
`install-windows.ps1`.
`NEXT_COOKIE_SECURE=true` предполагает HTTPS перед reverse proxy; прямой
HTTP-доступ к 8010 предназначен только для локальной диагностики.

Для одного `DataPath` поддерживается только один процесс candidate-приложения.
Не запускайте второй launcher и не добавляйте Uvicorn workers: durable writer
общего ZeusDB-индекса рассчитан на единственный сервисный процесс. Администратор
может повторить незавершённую обработку outbox через
`POST /api/v1/admin/voice-index/reconcile`; запрос не раскрывает чужие тексты,
аудио или эмбеддинги.
