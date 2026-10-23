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
файлов. Выполняйте BAT в PowerShell от имени администратора: после установки
моделей он автоматически зарегистрирует и запустит Windows-службу кандидата.

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

Основной установщик выдаёт Modify на отдельный `DataPath` текущей
учётной записи, выполняющей установку: это требуется для первоначального
создания администратора и ручного launcher. Для другого оператора при прямом
запуске `install-windows.ps1` передайте
`-ServiceAccount "DOMAIN\service-user"`. В стандартном Git-потоке служба
после этого отдельно получает минимальный доступ как `LocalService`; legacy
data ей не выдаются.
При первой установке используется `NEXT_COOKIE_SECURE=false`, потому что candidate слушает
только `127.0.0.1:8010` и его сначала проверяют по локальному HTTP. После
подключения HTTPS reverse proxy вручную установите `NEXT_COOKIE_SECURE=true` в
`candidate.env.ps1`, затем перезапустите службу. При обновлении установленное
значение сохраняется.

## Автоматический запуск как Windows-службы

Git-установщик делает это автоматически. Отдельная команда нужна только если
кандидат уже был подготовлен через `install-windows.ps1` без BAT, либо если
нужно повторно зарегистрировать службу после ручного ремонта. Она не трогает
legacy-службу на порту 8000. Откройте PowerShell от имени администратора:

```powershell
& "C:\Services\TranscriptionNext\apps\local-voice-transcriber\deploy\install-windows-service.ps1" `
  -ApplicationRoot "C:\Services\TranscriptionNext\apps\local-voice-transcriber" `
  -DataRoot "E:\TranscriptionData"
```

Сценарий загрузит WinSW 2.12.0 из официального release, сверит SHA-256,
создаст службу `TranscriptionNextCandidateService`, выдаст встроенной
низкопривилегированной учётной записи `LocalService` доступ только к candidate
data root, к каталогу приложения и к используемому Python runtime, а затем
запустит службу и проверит `http://127.0.0.1:8010/health`, включая идентификатор
сервиса и экземпляра. Пароль
Windows не запрашивается и не хранится в XML или Git.

Python 3.12 для кандидата устанавливается в `DataPath\python-runtime`, а не в
профиль интерактивного пользователя. Поэтому служба не зависит от `uv` или
Python в `C:\Users\...` и продолжает запускаться после выхода оператора из
Windows.

Каждая установка останавливает существующую candidate-службу перед заменой
окружения; Git-обновление также останавливает её перед `pull`. Для повторной
установки перестраивается только `ApplicationRoot\.venv`, пользовательские
данные и уже проверенные модели сохраняются. При сбое подготовки служба
остаётся остановленной до успешного повторного запуска установки.

Установщик явно выбирает Python из `DataPath\python-runtime` и закрепляет
`UV_PROJECT_ENVIRONMENT` за `ApplicationRoot\.venv`, даже если оператор
работает из другого активного окружения. Приложение устанавливается в
`site-packages` через `uv sync --locked --no-dev --no-editable --link-mode copy`.
При каждом обновлении локальный пакет кандидата принудительно пересобирается,
после чего установщик сравнивает установленные `index.html`, `app.js` и
`styles.css` с исходниками. Поэтому кэш `uv` не может оставить старый интерфейс
при уже обновлённом репозитории. Это устраняет зависимость от editable-путей и
разрешений файлов в пользовательском кэше `uv`. До загрузки моделей установщик
проверяет импорт тем же абсолютным `python.exe`, который будет запускать служба.

Launcher использует `python.exe -I -X utf8 -u -m local_voice_transcriber` и
передаёт код завершения Python в WinSW. Режим `-I` исключает текущий каталог,
пользовательские пакеты, `PYTHONPATH` и `PYTHONHOME` из поиска модулей; добавлять
`src` в `PYTHONPATH` не нужно. Эти свойства подтверждены в документации
[Python](https://docs.python.org/3.12/using/cmdline.html#cmdoption-I) и
[uv](https://docs.astral.sh/uv/guides/integration/docker/#non-editable-installs).

Служба получает Modify на рабочие данные, но только Read/Execute на приложение,
изолированный Python, launcher, конфигурацию и каталог WinSW. Для этих путей
устраняется унаследованное разрешение Modify; журналы WinSW остаются доступными
для записи. Установщик не выдаёт разрешений на Python в `C:\Users\...`:
старое окружение с таким runtime должно быть перестроено основным установщиком.

## Восстановление после сбоя запуска

Ошибки `uv-trampoline ... Permission denied` и `No module named
local_voice_transcriber` требуют повторной полной установки обновлённой версии.
Одна перерегистрация WinSW не исправляет содержимое старого `.venv`. Выполните
из PowerShell от имени администратора:

```powershell
$app = "C:\Services\TranscriptionNext\apps\local-voice-transcriber"
& "$app\deploy\install-windows.ps1" -RepositoryPath $app -DataPath "E:\TranscriptionData"
if (-not $?) { throw "Подготовка кандидата завершилась ошибкой" }
& "$app\deploy\install-windows-service.ps1" -ApplicationRoot $app -DataRoot "E:\TranscriptionData"
```

Для автоматического обновления используйте прежнюю команду BAT с `--update`.
Ручной экземпляр кандидата необходимо предварительно завершить: при занятом
порту установщик службы остановится, не завершая посторонние процессы.

Если после успешного обновления на `http://127.0.0.1:8010` виден прежний
интерфейс, не нужно удалять данные или вручную очищать `.venv`. Повторите
обычную команду Git-установщика с `--update`: она заново соберёт пакет
кандидата и завершится ошибкой, а не запустит службу, если установленные файлы
интерфейса отличаются от исходников. После успешной установки эта проверка
должна вывести `Файлы интерфейса проверены.`.

Перед новым запуском службы текущие `.err.log`, `.out.log` и `.wrapper.log`
перемещаются в `windows-service\logs\previous-<UTC>-<идентификатор>`. Поэтому
сообщение о неудаче показывает только журналы новой попытки, её идентификатор,
пути Python и runtime. При ошибке команды start, выходе процесса или провале
health установщик останавливает candidate и выводит stderr, stdout и журнал
WinSW. Старые журналы сохраняются в архивном каталоге для диагностики.

Проверка и диагностика:

```powershell
Get-Service TranscriptionNextCandidateService
Invoke-RestMethod "http://127.0.0.1:8010/health"
Get-Content "E:\TranscriptionData\windows-service\logs\TranscriptionNextCandidateService.out.log" -Tail 100
```

Для одного `DataPath` поддерживается только один процесс candidate-приложения.
Не запускайте второй launcher и не добавляйте Uvicorn workers: durable writer
общего ZeusDB-индекса рассчитан на единственный сервисный процесс. Администратор
может повторить незавершённую обработку outbox через
`POST /api/v1/admin/voice-index/reconcile`; запрос не раскрывает чужие тексты,
аудио или эмбеддинги.
