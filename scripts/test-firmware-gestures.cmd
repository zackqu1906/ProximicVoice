@echo off
setlocal
cd /d "%~dp0.."
if not exist ".runtime\venv\Scripts\python.exe" (
    echo Project Python is missing. Run scripts\setup.ps1 first.
    exit /b 1
)
".runtime\venv\Scripts\python.exe" -u tools\test_firmware_gestures.py %*
exit /b %errorlevel%
