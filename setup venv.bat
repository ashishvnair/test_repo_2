@echo off
echo.
echo ========================================
echo   RCA Platform -- Virtual Env Setup
echo ========================================
echo.

set SCRIPT_DIR=%~dp0
set VENV_PATH=%SCRIPT_DIR%.venv
set VENV_PYTHON=%VENV_PATH%\Scripts\python.exe
set VENV_PIP=%VENV_PATH%\Scripts\pip.exe

REM ── Find a system Python to create the venv ──────────────────────────────
if exist "%VENV_PYTHON%" (
    echo   .venv found at: %VENV_PATH%
    echo   Skipping creation, going straight to requirements install.
    goto :install
)

echo   .venv not found -- creating at: %VENV_PATH%
echo.

REM Try py launcher first (most reliable on Windows), then python3, then python
py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    py -3 -m venv "%VENV_PATH%"
    goto :check_venv
)

python3 --version >nul 2>&1
if %errorlevel% equ 0 (
    python3 -m venv "%VENV_PATH%"
    goto :check_venv
)

python --version >nul 2>&1
if %errorlevel% equ 0 (
    python -m venv "%VENV_PATH%"
    goto :check_venv
)

echo [ERROR] No Python found. Install Python 3.9+ from https://python.org and re-run.
pause
exit /b 1

:check_venv
if not exist "%VENV_PYTHON%" (
    echo [ERROR] venv creation failed -- check Python installation.
    pause
    exit /b 1
)
echo   .venv created successfully.
echo.

:install
echo   Installing / updating requirements...
echo.

REM ── Upgrade pip silently ─────────────────────────────────────────────────
echo   [1/3] Upgrading pip...
"%VENV_PIP%" install --upgrade pip --quiet
echo         done.

REM ── MCP server deps ──────────────────────────────────────────────────────
echo   [2/3] Installing mcp_server requirements...
"%VENV_PIP%" install -r "%SCRIPT_DIR%mcp_server\requirements.txt" --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install mcp_server requirements.
    pause
    exit /b 1
)
echo         done.

REM ── API server deps ───────────────────────────────────────────────────────
echo   [3/3] Installing api_server requirements...
"%VENV_PIP%" install -r "%SCRIPT_DIR%api_server\requirements.txt" --quiet
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install api_server requirements.
    pause
    exit /b 1
)
echo         done.

echo.
echo ========================================
echo   Setup complete.
echo   Next steps:
echo     1. Run  "start local.bat"   to start the RCA engine locally
echo     2. Run  "run tests.bat"     to verify all components
echo ========================================
echo.
pause
