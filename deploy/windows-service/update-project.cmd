@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_SCRIPT=%SCRIPT_DIR%update-project.ps1"

if not exist "%POWERSHELL_SCRIPT%" (
  echo [ОШИБКА] Не найден файл: %POWERSHELL_SCRIPT%
  exit /b 2
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%POWERSHELL_SCRIPT%" %*
set "UPDATE_EXIT_CODE=%ERRORLEVEL%"

if not "%UPDATE_EXIT_CODE%"=="0" (
  echo.
  echo Обновление завершилось с ошибкой. Путь к журналу указан выше.
)

exit /b %UPDATE_EXIT_CODE%
