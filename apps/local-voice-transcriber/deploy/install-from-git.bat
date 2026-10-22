@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Keep this file ASCII-only: cmd.exe parses batch text before code page changes.
rem The installer never modifies the legacy application or an existing dirty clone.

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
call :fail "Unknown parameter: %~1"
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
call :fail "git was not found. Install Git for Windows and try again."
goto :end

:check_powershell
where powershell >nul 2>nul
if not errorlevel 1 goto :clone_or_verify
call :fail "powershell was not found. Windows PowerShell 5.1 or newer is required."
goto :end

:clone_or_verify
if exist "%CLONE_PATH%" goto :existing_clone

for %%I in ("%CLONE_PATH%") do set "CLONE_PARENT=%%~dpI"
if exist "%CLONE_PARENT%" goto :clone
mkdir "%CLONE_PARENT%"
if not errorlevel 1 goto :clone
call :fail "Could not create the clone parent directory: %CLONE_PARENT%"
goto :end

:clone
echo Cloning branch "%BRANCH_NAME%" into "%CLONE_PATH%"...
git clone --branch "%BRANCH_NAME%" --single-branch -- "%REPOSITORY_URL%" "%CLONE_PATH%"
if not errorlevel 1 goto :verify_application
call :fail "Could not clone the requested branch. The destination directory was kept for diagnosis."
goto :end

:existing_clone
if exist "%CLONE_PATH%\.git" goto :verify_origin
call :fail "The directory already exists but is not a Git clone: %CLONE_PATH%. Choose another path."
goto :end

:verify_origin
set "ORIGIN_URL="
for /f "usebackq delims=" %%R in (`git -C "%CLONE_PATH%" remote get-url origin 2^>nul`) do if not defined ORIGIN_URL set "ORIGIN_URL=%%R"
if not defined ORIGIN_URL goto :missing_origin
if /I "%ORIGIN_URL%"=="%REPOSITORY_URL%" goto :verify_branch
call :fail "The existing clone origin does not match the requested repository. Installation stopped."
goto :end

:missing_origin
call :fail "The existing Git clone has no origin remote. Installation stopped."
goto :end

:verify_branch
set "CURRENT_BRANCH="
for /f "usebackq delims=" %%B in (`git -C "%CLONE_PATH%" branch --show-current 2^>nul`) do if not defined CURRENT_BRANCH set "CURRENT_BRANCH=%%B"
if not defined CURRENT_BRANCH goto :missing_branch
if /I "%CURRENT_BRANCH%"=="%BRANCH_NAME%" goto :maybe_update
call :fail "The existing clone uses another branch: %CURRENT_BRANCH%. Installation stopped."
goto :end

:missing_branch
call :fail "The existing clone is in detached HEAD state. Installation stopped."
goto :end

:maybe_update
if "%UPDATE_REQUESTED%"=="1" goto :check_worktree
goto :verify_application

:check_worktree
git -C "%CLONE_PATH%" status --porcelain >nul 2>nul
if not errorlevel 1 goto :read_worktree_status
call :fail "Could not check the existing Git clone status."
goto :end

:read_worktree_status
set "WORKTREE_DIRTY="
for /f "usebackq delims=" %%S in (`git -C "%CLONE_PATH%" status --porcelain 2^>nul`) do set "WORKTREE_DIRTY=1"
if not defined WORKTREE_DIRTY goto :pull_update
call :fail "The Git clone has uncommitted or untracked changes. Update stopped."
goto :end

:pull_update
echo Safely updating branch "%BRANCH_NAME%"...
git -C "%CLONE_PATH%" pull --ff-only origin "%BRANCH_NAME%"
if not errorlevel 1 goto :verify_application
call :fail "git pull --ff-only failed. The working directory was not overwritten."
goto :end

:verify_application
set "APPLICATION_ROOT=%CLONE_PATH%\apps\local-voice-transcriber"
set "INSTALLER_PATH=%APPLICATION_ROOT%\deploy\install-windows.ps1"
if exist "%INSTALLER_PATH%" goto :run_installer
call :fail "The Transcription Next installer was not found in the clone: %INSTALLER_PATH%"
goto :end

:run_installer
echo Starting the isolated Transcription Next installer...
powershell -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER_PATH%" -RepositoryPath "%APPLICATION_ROOT%" -DataPath "%DATA_PATH%" -Port %PORT%
if not errorlevel 1 goto :run_service_installer
call :fail "The PowerShell installer failed. The legacy application was not changed."
goto :end

:run_service_installer
set "SERVICE_INSTALLER_PATH=%APPLICATION_ROOT%\deploy\install-windows-service.ps1"
if exist "%SERVICE_INSTALLER_PATH%" goto :install_service
call :fail "The Transcription Next Windows service installer was not found: %SERVICE_INSTALLER_PATH%"
goto :end

:install_service
echo Registering the isolated Transcription Next Windows service...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SERVICE_INSTALLER_PATH%" -ApplicationRoot "%APPLICATION_ROOT%" -DataRoot "%DATA_PATH%"
if not errorlevel 1 goto :complete
call :fail "The Transcription Next Windows service installation failed. Run this BAT from an elevated PowerShell session."
goto :end

:complete
echo.
echo Transcription Next installation and Windows service registration completed.
goto :success

:help
call :print_usage
goto :success

:usage
call :print_usage
goto :end

:print_usage
echo.
echo Usage:
echo   %~nx0 ^<git-repository-url^> ^<clone-directory^> ^<data-directory^> ^<branch^> [port] [--update]
echo.
echo First installation example:
echo   %~nx0 "https://github.com/organization/transcription.git" "C:\Apps\Transcription" "C:\ProgramData\TranscriptionNext" "codex/transcription-next"
echo.
echo --update allows only git pull --ff-only for a clean clone with the same origin and branch.
exit /b 0

:fail
echo.
echo ERROR: %~1
exit /b 1

:success
exit /b 0

:end
exit /b 1
