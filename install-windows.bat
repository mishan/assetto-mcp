@echo off
REM Double-clickable wrapper for install-windows.ps1.
REM
REM Windows blocks .ps1 scripts from running on double-click by default
REM (execution policy). This .bat launches PowerShell with that policy
REM bypassed for this one script only - it does not change any system setting.
REM
REM Just double-click this file. Any arguments are passed through, e.g.:
REM     install-windows.bat -AcPath "D:\Games\steamapps\common\assettocorsa"

setlocal
set "SCRIPT=%~dp0install-windows.ps1"

if not exist "%SCRIPT%" (
    echo Could not find install-windows.ps1 next to this file.
    echo Keep both files together in the repo folder.
    pause
    exit /b 1
)

REM Tells the script not to prompt "Press Enter" itself - we pause below.
set "AC_INSTALLER_LAUNCHED_FROM_BAT=1"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo Installer exited with code %RC%.
pause

endlocal & exit /b %RC%
