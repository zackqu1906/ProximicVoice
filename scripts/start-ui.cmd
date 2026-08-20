@echo off
setlocal
cd /d "%~dp0.."

set "PIP_CACHE_DIR=%CD%\.cache\pip"
set "MODELSCOPE_CACHE=%CD%\.cache\modelscope"
set "HF_HOME=%CD%\.cache\huggingface"
set "TORCH_HOME=%CD%\.cache\torch"
if not defined PROXIMIC_LLM_HOME set "PROXIMIC_LLM_HOME=%CD%\.runtime\local-llm"

if not exist ".runtime\venv\Scripts\python.exe" (
    echo Proximic Voice is not installed yet.
    echo Run: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
    pause
    exit /b 1
)

".runtime\venv\Scripts\python.exe" -m proximic_ring.ui
if errorlevel 1 (
    echo.
    echo Proximic Voice exited with an error.
    echo Reinstall with: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Recreate
    pause
    exit /b 1
)
endlocal
