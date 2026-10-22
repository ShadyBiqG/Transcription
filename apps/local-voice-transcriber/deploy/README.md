# Установка на Windows Server

Запустите install-windows.ps1 из этого каталога. Пример:

    powershell -ExecutionPolicy Bypass -File .\deploy\install-windows.ps1 -RepositoryPath . -DataPath C:\ProgramData\TranscriptionNext

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
