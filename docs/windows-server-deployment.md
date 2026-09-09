# Развертывание сервиса транскрибации на Windows Server

Эта инструкция устанавливает приложение как настоящую Windows-службу: после перезагрузки сервера оно запускается автоматически, не требует входа пользователя и доступно компьютерам локальной сети.

## 1. Как устроена передача файла

Если браузер открыт на компьютере пользователя, а noScribe установлена на сервере, общий диск не требуется.

```text
Компьютер пользователя                    Windows-сервер

meeting.webm
    │
    │ HTTP multipart upload
    └───────────────────────────────────> data/tmp/<job-id>/source.webm
                                             │ проверка размера и SHA-256
                                             ▼
                                         data/jobs/<job-id>/source.webm
                                             │ локальный запуск noScribe
                                             ▼
                                         transcript.vtt + manifest.json
    <───────────────────────────────────────┘ скачивание результата
```

Браузер передает содержимое файла, а не локальный путь `C:\...`. Путь на компьютере пользователя серверу недоступен и не нужен. Приложение принимает поток частями по 1 МБ и сразу пишет его на диск сервера, поэтому весь большой файл не держится в оперативной памяти. После ответа `202 Accepted` вкладку можно закрыть: файл уже скопирован на сервер, а фоновая очередь продолжит обработку. Для записи 74 МБ по сети 100 Мбит/с сама загрузка обычно занимает несколько секунд; транскрибация выполняется уже на сервере.

HTTP не шифрует запись. Bearer-токен ограничивает доступ, но не заменяет TLS. Используйте сервис только в доверенной локальной сети/VPN либо поставьте перед ним HTTPS reverse proxy. Не перенаправляйте порт 8000 на интернет.

## 2. Требования к серверу

- Windows 10/11 или Windows Server x64.
- Локальный NTFS-каталог для приложения и данных; не используйте подключенный пользователем сетевой диск — службы обычно не видят такие буквы дисков.
- Python 3.12+ и `uv`.
- [noScribe](https://noscribe.de/en/) 0.7.2 с установленными моделями.
- Отдельная либо существующая Windows-учетная запись, под которой noScribe уже запускалась вручную.
- Права администратора для регистрации службы и правила брандмауэра.

Рекомендуемые каталоги:

```text
C:\Services\Transcription       приложение
D:\TranscriptionData            записи, SQLite, VTT и журналы заданий
```

Если диска `D:` нет, используйте `C:\TranscriptionData`.

noScribe должна быть установлена именно на сервере, где работает служба. Ее наличие только на компьютере пользователя недостаточно: после загрузки сервер запускает собственный локальный `noScribe.exe`.

## 3. Подготовка noScribe под учетной записью службы

Войдите на сервер под учетной записью, от которой будет работать служба, и один раз запустите noScribe. Убедитесь, что модели установлены и обычная тестовая транскрибация работает.

Проверьте CLI в PowerShell:

```powershell
& 'C:\Program Files (x86)\noScribe\noScribe.exe' --help
& 'C:\Program Files (x86)\noScribe\noScribe.exe' --help-models
```

Ожидаются модели `fast` и `precise`. Конфигурация noScribe хранится в профиле текущего пользователя, поэтому запуск службы от `LocalSystem` не рекомендуется.

## 4. Установка приложения

Клонируйте **весь репозиторий** в постоянный каталог. Нельзя копировать только `src`: команда `uv sync` также требует корневые файлы `pyproject.toml` и `uv.lock`.

Если каталог `C:\Services\Transcription` еще не существует, выполните:

```powershell
Set-Location 'C:\Services'
git clone 'https://github.com/ShadyBiqG/Transcription.git' 'Transcription'
```

Если Git на сервере не установлен, скачайте ZIP репозитория и распакуйте **его содержимое** в `C:\Services\Transcription`. После копирования структура должна начинаться так:

```text
C:\Services\Transcription\pyproject.toml
C:\Services\Transcription\uv.lock
C:\Services\Transcription\src\
C:\Services\Transcription\deploy\
```

Откройте обычный PowerShell под учетной записью службы:

```powershell
Set-Location 'C:\Services\Transcription'
if (-not (Test-Path '.\pyproject.toml')) {
    throw 'Проект скопирован не полностью: отсутствует C:\Services\Transcription\pyproject.toml'
}
uv sync --frozen --no-dev
Copy-Item '.env.example' '.env'
New-Item -ItemType Directory -Path 'D:\TranscriptionData' -Force
```

Сгенерируйте токен:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Откройте `.env` и задайте как минимум:

```dotenv
TRANSCRIPTION_DATA_DIR=D:\TranscriptionData
TRANSCRIPTION_NOSCRIBE_PATH=C:\Program Files (x86)\noScribe\noScribe.exe
TRANSCRIPTION_DEFAULT_LANGUAGE=ru
TRANSCRIPTION_DEFAULT_MODEL=precise
TRANSCRIPTION_ALLOWED_MODELS=fast,precise
TRANSCRIPTION_MAX_UPLOAD_BYTES=5368709120
TRANSCRIPTION_NOSCRIBE_TIMEOUT_SECONDS=21600
TRANSCRIPTION_API_TOKEN=ВСТАВЬТЕ_СГЕНЕРИРОВАННЫЙ_ТОКЕН
```

Токен должен быть одинаковым только у сервера и пользователей сервиса. Не добавляйте `.env` в Git и не отправляйте токен в переписке.

## 5. Проверка до регистрации службы

Запустите приложение вручную именно из виртуального окружения:

```powershell
Set-Location 'C:\Services\Transcription'
& '.\.venv\Scripts\python.exe' -m transcription_service --host 127.0.0.1 --port 8000
```

В другом окне сервера проверьте:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/health'
```

Ожидаемый результат:

```text
service  : ok
noscribe : ready
models   : {fast, precise}
worker   : running
```

Остановите ручной процесс сочетанием `Ctrl+C`.

## 6. Регистрация Windows-службы через WinSW

Используется стабильный WinSW 2.12.0. Скачивайте бинарный файл только с официальной страницы релиза.

Откройте PowerShell **от имени администратора**:

```powershell
Set-Location 'C:\Services\Transcription\deploy\windows-service'
Invoke-WebRequest `
  'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe' `
  -OutFile 'TranscriptionService.exe'
New-Item -ItemType Directory -Path '.\logs' -Force
.\TranscriptionService.exe install
```

Файлы `TranscriptionService.exe` и `TranscriptionService.xml` должны находиться рядом. XML уже настроен на:

- автоматический отложенный старт;
- `0.0.0.0:8000`;
- Python из `.venv` проекта;
- рабочий каталог проекта, где читается `.env`;
- автоматический перезапуск через 10 секунд;
- ротацию служебных логов.

### Назначение учетной записи

До первого старта откройте `services.msc`:

1. Найдите **Local Transcription Service**.
2. Откройте **Свойства → Вход в систему**.
3. Выберите **С учетной записью**.
4. Укажите учетную запись, под которой проверялась noScribe, например `SERVER\TranscriptionUser`.
5. Введите пароль и примените изменения.

Учетной записи нужны:

- чтение приложения и `.venv`;
- чтение/запуск `noScribe.exe` и моделей;
- изменение `D:\TranscriptionData`;
- изменение `deploy\windows-service\logs`;
- право **Log on as a service**.

После назначения учетной записи:

```powershell
.\TranscriptionService.exe start
.\TranscriptionService.exe status
Get-Service 'LocalTranscriptionService'
```

Состояние должно быть `Running`. После перезагрузки Windows служба запустится автоматически; вручную приложение запускать не нужно.

## 7. Настройка Windows Defender Firewall

Сначала проверьте сетевой профиль сервера:

```powershell
Get-NetConnectionProfile
```

Правило ниже активно только для профиля `Private` и принимает соединения только из локальной подсети. Выполните от имени администратора:

```powershell
$pythonExe = 'C:\Services\Transcription\.venv\Scripts\python.exe'
New-NetFirewallRule `
  -DisplayName 'Local Transcription Service TCP 8000' `
  -Description 'Доступ к сервису транскрибации только из локальной подсети' `
  -Direction Inbound `
  -Action Allow `
  -Protocol TCP `
  -LocalPort 8000 `
  -Program $pythonExe `
  -Profile Private `
  -RemoteAddress LocalSubnet
```

Если сервер состоит в домене и активен профиль `DomainAuthenticated`, замените `-Profile Private` на `-Profile Domain,Private`. Не включайте `Public` без отдельного анализа безопасности.

Если клиенты находятся в другой VLAN/подсети, `LocalSubnet` может их не пропустить. Вместо него укажите разрешенные сети явно, например:

```powershell
-RemoteAddress '192.168.10.0/24','192.168.20.0/24'
```

Проверка правила:

```powershell
Get-NetFirewallRule -DisplayName 'Local Transcription Service TCP 8000'
Get-NetTCPConnection -State Listen -LocalPort 8000
```

## 8. Подключение с другого компьютера

На сервере определите IPv4-адрес:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.AddressState -eq 'Preferred' } |
  Select-Object InterfaceAlias,IPAddress
```

На клиентском компьютере проверьте порт:

```powershell
Test-NetConnection -ComputerName '<IP-СЕРВЕРА>' -Port 8000
```

Затем откройте в браузере:

```text
http://<IP-СЕРВЕРА>:8000
```

Введите API-токен в поле интерфейса, выберите локальный `.webm` и нажмите **Запустить**. Файл будет загружен на сервер. После ответа о принятии можно закрыть вкладку и вернуться позднее с любого разрешенного компьютера.

## 9. Где находятся данные

При `TRANSCRIPTION_DATA_DIR=D:\TranscriptionData`:

```text
D:\TranscriptionData\jobs.sqlite3
D:\TranscriptionData\jobs\<job-id>\source.webm
D:\TranscriptionData\jobs\<job-id>\transcript.vtt
D:\TranscriptionData\jobs\<job-id>\manifest.json
D:\TranscriptionData\jobs\<job-id>\noscribe.log
```

Исходная запись полностью копируется на сервер. noScribe не обращается к диску клиентского компьютера.

На текущем этапе автоматическая очистка не реализована: исходный `.webm` и результаты остаются на сервере, пока администратор не удалит каталог задания. Учитывайте это при расчете свободного места и настройке резервного копирования.

## 10. Обслуживание и диагностика

```powershell
Set-Location 'C:\Services\Transcription\deploy\windows-service'
.\TranscriptionService.exe status
.\TranscriptionService.exe restart
.\TranscriptionService.exe stop
```

Журналы оболочки службы находятся в `deploy\windows-service\logs`; журнал конкретной транскрибации — в каталоге задания.

После обновления кода:

```powershell
.\TranscriptionService.exe stop
Set-Location 'C:\Services\Transcription'
uv sync --frozen --no-dev
Set-Location '.\deploy\windows-service'
.\TranscriptionService.exe start
```

Удаление службы, если понадобится:

```powershell
.\TranscriptionService.exe stop
.\TranscriptionService.exe uninstall
```

Правило брандмауэра удаляется отдельно:

```powershell
Remove-NetFirewallRule -DisplayName 'Local Transcription Service TCP 8000'
```

## 11. Типовые проблемы

### `No pyproject.toml found in current directory or any parent directory`

Команда запущена не из корня приложения либо на сервер скопирована только часть репозитория. Проверьте:

```powershell
Set-Location 'C:\Services\Transcription'
Get-Item '.\pyproject.toml', '.\uv.lock'
```

Если файлов нет, повторно выполните шаг 4 и склонируйте или распакуйте весь репозиторий. Не создавайте пустой `pyproject.toml` вручную: серверу нужен файл из этого проекта. После появления обоих файлов повторите:

```powershell
uv sync --frozen --no-dev
```

### `noscribe: unavailable`

- Проверьте путь в `.env`.
- Запустите `noScribe.exe --help-models` под учетной записью службы.
- Убедитесь, что профиль этой учетной записи и модели доступны без интерактивного GUI.

### Сервис работает на сервере, но недоступен с клиента

- Проверьте, что служба запускает приложение с `--host 0.0.0.0`.
- Проверьте `Get-NetTCPConnection -LocalPort 8000`.
- Проверьте профиль сети и firewall rule.
- Проверьте маршрутизацию/VLAN через `Test-NetConnection`.
- Не используйте `localhost` на клиенте: это адрес самого клиента, а не сервера.

### HTTP 401

Введен неверный API-токен. Значение должно полностью совпадать с `TRANSCRIPTION_API_TOKEN` на сервере.

### Загрузка прошла, но транскрибация не начинается

- Проверьте свободное место в каталоге данных.
- Проверьте статус задания и `noscribe.log`.
- Убедитесь, что noScribe не запущена отдельно и GPU/память доступны.
