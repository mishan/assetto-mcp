@echo off
REM Double-clickable wrapper for diagnose.ps1.
REM
REM Windows blocks .ps1 scripts from running on double-click by default
REM (execution policy). This .bat launches PowerShell with that policy
REM bypassed for this one script only - it changes no system setting.

setlocal
set "SCRIPT=%~dp0diagnose.ps1"

if not exist "%SCRIPT%" (
    echo Could not find diagnose.ps1 next to this file.
    echo Keep both files together in the repo folder.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo Diagnostics exited with code %RC%.
pause

endlocal & exit /b %RC%
