# Установка на Windows Server

Запустите install-windows.ps1 из этого каталога. Пример:

    powershell -ExecutionPolicy Bypass -File .\deploy\install-windows.ps1 -RepositoryPath . -DataPath C:\ProgramData\TranscriptionNext

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
& "C:\Users\AI\install-from-git.bat" "https://github.com/ShadyBiqG/Transcription.git" "C:\Services\TranscriptionNext" "C:\ProgramData\TranscriptionNext" "codex/transcription-next"
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
candidate.env.ps1 вне репозитория. При первом запуске он запрашивает логин и
пароль первого администратора, создаёт его через CLI приложения и не сохраняет
пароль в файле конфигурации. Для unattended-установки передайте
BootstrapAdminLogin и BootstrapAdminPassword (SecureString).

Доставьте model packs в созданный каталог models до запуска и назначьте
отдельную service account без ACL на legacy data. `NEXT_COOKIE_SECURE=true`
предполагает HTTPS перед reverse proxy; прямой HTTP-доступ к 8010 предназначен
только для локальной диагностики.

Для одного `DataPath` поддерживается только один процесс candidate-приложения.
Не запускайте второй launcher и не добавляйте Uvicorn workers: durable writer
общего ZeusDB-индекса рассчитан на единственный сервисный процесс. Администратор
может повторить незавершённую обработку outbox через
`POST /api/v1/admin/voice-index/reconcile`; запрос не раскрывает чужие тексты,
аудио или эмбеддинги.
