@echo off
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Proximic Voice is not installed yet.
    echo Run: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
    exit /b 1
)

".venv\Scripts\python.exe" -m proximic_ring.ui
endlocal
