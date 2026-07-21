@echo off
REM Code Scanner Autostart Management - Windows (Task Scheduler)
REM Usage: autostart-windows.bat [install|remove|status] "<cli_command>"
REM Example: autostart-windows.bat install "C:\path\to\project1 -c C:\path\to\config1 C:\path\to\project2 -c C:\path\to\config2 --mode branch"

setlocal enabledelayedexpansion

set "TASK_NAME=CodeScanner"
set "SCRIPT_DIR=%~dp0"

if "%~1"=="" goto :usage
if "%~1"=="install" goto :precheck
if "%~1"=="remove" goto :precheck
if "%~1"=="status" goto :precheck
goto :usage

:precheck
where code-scanner >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('where code-scanner') do set "INSTALLED_PATH=%%p"
    for /f "delims=" %%v in ('code-scanner --version 2^>nul') do set "INSTALLED_VERSION=%%v"
    echo [INFO] Installed code-scanner: %INSTALLED_PATH% (%INSTALLED_VERSION%)
) else (
    echo [INFO] code-scanner is not currently installed on PATH.
)

set "WRAPPER_SCRIPT=%USERPROFILE%\.code-scanner\launch-wrapper.bat"
if exist "%WRAPPER_SCRIPT%" (
    set "CURRENT_CMD="
    for /f "usebackq skip=3 delims=" %%a in ("%WRAPPER_SCRIPT%") do set "CURRENT_CMD=%%a"
    if not "!CURRENT_CMD!"=="" (
        set "CLI_CMD=!CURRENT_CMD:code-scanner =!"
        set "CLI_CMD=!CLI_CMD:uv run code-scanner =!"
        echo.
        echo [INFO] Current ^<cli_command^> is "!CLI_CMD!"
    )
)

if "%~1"=="install" goto :install
if "%~1"=="remove" goto :remove
if "%~1"=="status" goto :status
goto :usage

:usage
echo Code Scanner Autostart Management - Windows
echo.
echo Usage: %~nx0 ^<command^> "<cli_command>"
echo.
echo Commands:
echo   install ^<cli_command^>  Install autostart task with full CLI command
echo   remove                      Remove autostart task
echo   status                      Check task status
echo.
echo Examples:
echo   %~nx0 install "C:\path\to\project1 -c C:\path\to\config1 C:\path\to\project2 -c C:\path\to\config2 --mode branch"
echo   %~nx0 remove
echo   %~nx0 status
exit /b 1

:install
if "%~2"=="" (
    echo [ERROR] Missing CLI command argument
    goto :usage
)

set "CLI_ARGS=%~2"

REM Find code-scanner
set "SCANNER_CMD="
where code-scanner >nul 2>&1 && set "SCANNER_CMD=code-scanner"
if "%SCANNER_CMD%"=="" (
    where uv >nul 2>&1 && set "SCANNER_CMD=uv run code-scanner"
)
if "%SCANNER_CMD%"=="" (
    echo [ERROR] Could not find code-scanner or uv. Please install code-scanner first.
    exit /b 1
)

REM Resolve project root (parent of this script's directory)
set "PROJECT_ROOT=%SCRIPT_DIR%.."

REM Reinstall from source so the on-PATH binary matches the source.
REM Prefer pipx (matches the standard install), fall back to uv/pip.
echo [INFO] Reinstalling code-scanner from source: %PROJECT_ROOT%
where pipx >nul 2>&1
if not errorlevel 1 (
    pipx install --force "%PROJECT_ROOT%" >nul 2>&1
    if errorlevel 1 (
        echo [WARNING] pipx reinstall failed; continuing with the current binary.
    ) else (
        for /f "delims=" %%v in ('code-scanner --version 2^>nul') do echo [SUCCESS] Reinstalled code-scanner: %%v
    )
) else (
    where uv >nul 2>&1
    if not errorlevel 1 (
        uv pip install "%PROJECT_ROOT%" >nul 2>&1
        if errorlevel 1 (
            echo [WARNING] uv reinstall failed; continuing with the current binary.
        )
    ) else (
        echo [WARNING] Neither pipx nor uv found; skipping reinstall. The service will use the current binary.
    )
)

REM Verify the installed binary supports every long flag (--mode, --commit, ...)
REM in the command. Uses PowerShell to scan --help output; emits a clear
REM diagnostic if a flag is unsupported (stale install) instead of a confusing
REM argparse error during test launch.
powershell -NoProfile -Command ^
  "$cmdArgs=' %CLI_ARGS% ';" ^
  "$help = code-scanner --help 2>&1 | Out-String;" ^
  "$missing = @();" ^
  "foreach ($m in [regex]::Matches($cmdArgs, '--[a-z][a-z0-9-]*')) {" ^
  "  $flag = $m.Value;" ^
  "  if ($help -notlike \"*$flag*\") { $missing += $flag }" ^
  "};" ^
  "if ($missing.Count -gt 0) {" ^
  "  Write-Host '';" ^
  "  Write-Host '[ERROR] Installed code-scanner does not support: ' ($missing -join ', ') -ForegroundColor Red;" ^
  "  Write-Host '[ERROR] This usually means the installed binary is older than the source.' -ForegroundColor Red;" ^
  "  Write-Host '[ERROR] Re-install from source, e.g.:' -ForegroundColor Red;" ^
  "  Write-Host '[ERROR]   pipx install --force \"%PROJECT_ROOT%\"' -ForegroundColor Red;" ^
  "  exit 1" ^
  "}"
if errorlevel 1 (
    exit /b 1
)

echo [INFO] Testing code-scanner launch...
echo [INFO] Command: %SCANNER_CMD% %CLI_ARGS%
echo.

REM Test launch - run for 5 seconds and capture output
set "TEST_OUTPUT=%TEMP%\code-scanner-test.txt"
start /b cmd /c ""%SCANNER_CMD%" %CLI_ARGS% 2>&1" > "%TEST_OUTPUT%" 2>&1
timeout /t 5 /nobreak >nul 2>&1

REM Kill any running code-scanner processes from test
taskkill /f /im code-scanner.exe >nul 2>&1
taskkill /f /im python.exe /fi "WINDOWTITLE eq code-scanner*" >nul 2>&1

REM Display output
if exist "%TEST_OUTPUT%" (
    type "%TEST_OUTPUT%"
    echo.

    REM Check for success indicators
    findstr /i "Scanner running Scanner loop started Scanner thread started" "%TEST_OUTPUT%" >nul 2>&1
    if not errorlevel 1 (
        echo [SUCCESS] Test launch succeeded - scanner started correctly.
        del "%TEST_OUTPUT%" >nul 2>&1
        goto :test_passed
    )

    REM Check for error indicators
    findstr /i "error failed exception traceback could not cannot refused" "%TEST_OUTPUT%" >nul 2>&1
    if not errorlevel 1 (
        echo [ERROR] Test launch failed. Please fix the issues above and try again.
        del "%TEST_OUTPUT%" >nul 2>&1
        exit /b 1
    )

    del "%TEST_OUTPUT%" >nul 2>&1
)

REM No clear success or failure - ask user
echo [WARNING] Could not automatically verify launch success.
echo [WARNING] Please check the output above and ensure code-scanner starts correctly.
set /p "RESPONSE=Continue with installation? (y/N): "
if /i not "%RESPONSE%"=="y" (
    echo [ERROR] Installation cancelled.
    exit /b 1
)

:test_passed

REM Check for existing task
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if not errorlevel 1 (
    echo [WARNING] Found existing autostart task.
    REM Try to read current command from wrapper script
    set "CURRENT_WRAPPER=%USERPROFILE%\.code-scanner\launch-wrapper.bat"
    if exist "!CURRENT_WRAPPER!" (
        echo.
        echo   Current: 
        REM Show last line of wrapper script (the actual command)
        for /f "usebackq delims=" %%a in ("!CURRENT_WRAPPER!") do set "CURRENT_CMD=%%a"
        echo   !CURRENT_CMD!
        echo   New:     %SCANNER_CMD% %CLI_ARGS%
        echo.
    )
    set /p "REPLACE=Replace existing configuration? (y/N): "
    if /i not "!REPLACE!"=="y" (
        echo [INFO] Installation cancelled.
        exit /b 0
    )
    echo [INFO] Removing existing task...
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
)

REM Create wrapper script with 60-second delay
set "HOME_DIR=%USERPROFILE%\.code-scanner"
if not exist "%HOME_DIR%" mkdir "%HOME_DIR%"

set "WRAPPER_SCRIPT=%HOME_DIR%\launch-wrapper.bat"
(
    echo @echo off
    echo REM Code Scanner launch wrapper with startup delay
    echo timeout /t 60 /nobreak ^>nul
    echo %SCANNER_CMD% %CLI_ARGS%
) > "%WRAPPER_SCRIPT%"

REM Create scheduled task to run at logon
echo [INFO] Creating scheduled task...
schtasks /create /tn "%TASK_NAME%" /tr "\"%WRAPPER_SCRIPT%\"" /sc onlogon /rl highest /f

if errorlevel 1 (
    echo [ERROR] Failed to create scheduled task.
    exit /b 1
)

echo [SUCCESS] Code Scanner autostart installed successfully!
echo.
echo [INFO] Useful commands:
echo   schtasks /query /tn "%TASK_NAME%"         # Check status
echo   schtasks /run /tn "%TASK_NAME%"           # Start manually
echo   schtasks /end /tn "%TASK_NAME%"           # Stop task
echo   schtasks /delete /tn "%TASK_NAME%" /f     # Remove task
exit /b 0

:remove
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] No autostart task found.
    exit /b 0
)

echo [INFO] Ending task if running...
schtasks /end /tn "%TASK_NAME%" >nul 2>&1

echo [INFO] Removing scheduled task...
schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1

echo [INFO] Removing wrapper script...
del "%USERPROFILE%\.code-scanner\launch-wrapper.bat" >nul 2>&1

echo [SUCCESS] Code Scanner autostart removed.
exit /b 0

:status
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] No autostart task configured.
    exit /b 0
)

echo [INFO] Scheduled task status:
schtasks /query /tn "%TASK_NAME%" /v /fo list
exit /b 0
