@echo off
setlocal
title Flight Wall - Windows to Batocera
echo Flight Wall Windows installer
echo.
echo This opens the supplied PowerShell helper in its own window/process.
echo Its execution-policy option applies only to this process; it does not change Windows policy.
echo No administrator window is required just to run the helper.
echo.
where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo Windows PowerShell was not found. Use the manual instructions in the supplied guide.
    echo.
    pause
    exit /b 1
)
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Install-Flight-Wall-Windows.ps1"
set "flightwall_exit_code=%ERRORLEVEL%"
echo.
if not "%flightwall_exit_code%"=="0" echo Installation did not complete. Read the error above before trying again.
pause
exit /b %flightwall_exit_code%
