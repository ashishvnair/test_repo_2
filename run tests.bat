@echo off
echo.
echo ========================================
echo   RCA Platform — Component Tests
echo ========================================
echo.

REM Enable ANSI colours in Windows console
reg query HKCU\Console /v VirtualTerminalLevel >nul 2>&1 || reg add HKCU\Console /v VirtualTerminalLevel /t REG_DWORD /d 1 /f >nul 2>&1

REM ── Resolve Python: prefer .venv in the same folder as this script ──────────
set SCRIPT_DIR=%~dp0
set VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe
set VENV_PIP=%SCRIPT_DIR%.venv\Scripts\pip.exe

if exist "%VENV_PYTHON%" (
    set PYTHON=%VENV_PYTHON%
    set PIP=%VENV_PIP%
    echo   Using venv : %VENV_PYTHON%
) else (
    REM Fall back to system Python
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] No .venv found and Python not in PATH.
        echo         Create a venv: python -m venv .venv
        pause
        exit /b 1
    )
    set PYTHON=python
    set PIP=python -m pip
    echo   Using system Python
)
echo.

REM Install psycopg2 if missing (needed for pgvector test)
REM Use "%PYTHON% -m pip" (not %PIP%) to guarantee install goes into the correct venv
"%PYTHON%" -c "import psycopg2" >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing psycopg2-binary for pgvector test...
    "%PYTHON%" -m pip install psycopg2-binary --quiet
)

REM ── Override env vars that differ when running outside Docker ─────────────
REM    .env stores host.docker.internal for LLM (used by containers).
REM    Tests run on the local machine, so localhost is correct here.
set LLM_BASE_URL=http://localhost:1234/v1
set MCP_SERVER_URL=http://localhost:8001
set PGVECTOR_DSN=postgresql://rca:rca@localhost:5432/rca_db

REM Run tests
"%PYTHON%" "%SCRIPT_DIR%test_components.py"
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE%==0 (
    echo All tests passed.
) else (
    echo Some tests failed. See details above.
)
echo.
pause
exit /b %EXIT_CODE%
