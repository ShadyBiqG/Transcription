@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

rem Установка Transcription Next из явно указанного Git-репозитория и ветки.
rem Скрипт не меняет legacy-приложение и не перезаписывает существующий клон.

set "REPOSITORY_URL=%~1"
set "CLONE_PATH=%~2"
set "DATA_PATH=%~3"
set "BRANCH_NAME=%~4"
set "PORT=8010"
set "PORT_WAS_SET=0"
set "UPDATE_REQUESTED=0"

if /I "%~1"=="/?" goto :help
if /I "%~1"=="--help" goto :help
if "%REPOSITORY_URL%"=="" goto :usage
if "%CLONE_PATH%"=="" goto :usage
if "%DATA_PATH%"=="" goto :usage
if "%BRANCH_NAME%"=="" goto :usage

shift
shift
shift
shift

:parse_optional_arguments
if "%~1"=="" goto :validate_arguments
if /I "%~1"=="--update" goto :accept_update
if "%PORT_WAS_SET%"=="0" goto :accept_port
call :fail "Неизвестный параметр: %~1"
goto :end

:accept_update
set "UPDATE_REQUESTED=1"
shift
goto :parse_optional_arguments

:accept_port
set "PORT=%~1"
set "PORT_WAS_SET=1"
shift
goto :parse_optional_arguments

:validate_arguments
where git >nul 2>nul
if not errorlevel 1 goto :check_powershell
call :fail "Не найдена команда git. Установите Git for Windows и повторите запуск."
goto :end

:check_powershell
where powershell >nul 2>nul
if not errorlevel 1 goto :clone_or_verify
call :fail "Не найдена команда powershell. Нужен Windows PowerShell 5.1 или новее."
goto :end

:clone_or_verify
if exist "%CLONE_PATH%" goto :existing_clone

for %%I in ("%CLONE_PATH%") do set "CLONE_PARENT=%%~dpI"
if exist "%CLONE_PARENT%" goto :clone
mkdir "%CLONE_PARENT%"
if not errorlevel 1 goto :clone
call :fail "Не удалось создать родительский каталог для клона: %CLONE_PARENT%"
goto :end

:clone
echo Клонирование ветки "%BRANCH_NAME%" в "%CLONE_PATH%"...
git clone --branch "%BRANCH_NAME%" --single-branch -- "%REPOSITORY_URL%" "%CLONE_PATH%"
if not errorlevel 1 goto :verify_application
call :fail "Не удалось клонировать указанную ветку. Каталог назначения сохранён для диагностики."
goto :end

:existing_clone
if exist "%CLONE_PATH%\.git" goto :verify_origin
call :fail "Каталог уже существует, но не является Git-клоном: %CLONE_PATH%. Укажите другой путь."
goto :end

:verify_origin
set "ORIGIN_URL="
for /f "usebackq delims=" %%R in (`git -C "%CLONE_PATH%" remote get-url origin 2^>nul`) do if not defined ORIGIN_URL set "ORIGIN_URL=%%R"
if not defined ORIGIN_URL goto :missing_origin
if /I "%ORIGIN_URL%"=="%REPOSITORY_URL%" goto :verify_branch
call :fail "URL origin существующего клона не совпадает с переданным репозиторием. Обновление и установка отменены."
goto :end

:missing_origin
call :fail "У существующего Git-клона не настроен origin. Установка отменена."
goto :end

:verify_branch
set "CURRENT_BRANCH="
for /f "usebackq delims=" %%B in (`git -C "%CLONE_PATH%" branch --show-current 2^>nul`) do if not defined CURRENT_BRANCH set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH goto :missing_branch
if /I "%CURRENT_BRANCH%"=="%BRANCH_NAME%" goto :maybe_update
call :fail "В существующем клоне активна другая ветка: %CURRENT_BRANCH%. Установка отменена."
goto :end

:missing_branch
call :fail "Существующий клон находится в detached HEAD. Установка отменена."
goto :end

:maybe_update
if "%UPDATE_REQUESTED%"=="1" goto :check_worktree
goto :verify_application

:check_worktree
git -C "%CLONE_PATH%" status --porcelain >nul 2>nul
if not errorlevel 1 goto :read_worktree_status
call :fail "Не удалось проверить состояние существующего Git-клона."
goto :end

:read_worktree_status
set "WORKTREE_DIRTY="
for /f "usebackq delims=" %%S in (`git -C "%CLONE_PATH%" status --porcelain 2^>nul`) do set "WORKTREE_DIRTY=1"
if not defined WORKTREE_DIRTY goto :pull_update
call :fail "В Git-клоне есть незакоммиченные или неотслеживаемые изменения. Обновление отменено."
goto :end

:pull_update
echo Безопасное обновление ветки "%BRANCH_NAME%"...
git -C "%CLONE_PATH%" pull --ff-only origin "%BRANCH_NAME%"
if not errorlevel 1 goto :verify_application
call :fail "Не удалось выполнить git pull --ff-only. Рабочая директория не перезаписывалась."
goto :end

:verify_application
set "APPLICATION_ROOT=%CLONE_PATH%\apps\local-voice-transcriber"
set "INSTALLER_PATH=%APPLICATION_ROOT%\deploy\install-windows.ps1"
if exist "%INSTALLER_PATH%" goto :run_installer
call :fail "В клоне не найден установщик Transcription Next: %INSTALLER_PATH%"
goto :end

:run_installer
echo Запуск изолированного установщика Transcription Next...
powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER_PATH%" -RepositoryPath "%APPLICATION_ROOT%" -DataPath "%DATA_PATH%" -Port %PORT%
if not errorlevel 1 goto :complete
call :fail "PowerShell-установщик завершился с ошибкой. Legacy-приложение не изменялось."
goto :end

:complete
echo.
echo Установка Transcription Next завершена.
goto :success

:help
call :print_usage
goto :success

:usage
call :print_usage
goto :end

:print_usage
echo.
echo Использование:
echo   %~nx0 ^<URL Git-репозитория^> ^<каталог-клона^> ^<каталог-данных^> ^<ветка^> [порт] [--update]
echo.
echo Пример первой установки:
echo   %~nx0 "https://github.com/organization/transcription.git" "C:\Apps\Transcription" "C:\ProgramData\TranscriptionNext" "codex/transcription-next"
echo.
echo --update разрешает только git pull --ff-only для чистого клона с тем же origin и активной веткой.
exit /b 0

:fail
echo.
echo ОШИБКА: %~1
exit /b 1

:success
exit /b 0

:end
exit /b 1
