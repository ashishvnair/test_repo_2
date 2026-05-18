@echo off
echo.
echo ========================================
echo   RCA Platform -- API Server  :8000
echo ========================================
echo.

set SCRIPT_DIR=%~dp0
set VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe
set VENV_UVICORN=%SCRIPT_DIR%.venv\Scripts\uvicorn.exe

REM ── Guard: venv must exist ────────────────────────────────────────────────
if not exist "%VENV_PYTHON%" (
    echo [ERROR] .venv not found at: %SCRIPT_DIR%.venv
    echo   Run "setup venv.bat" first.
    echo.
    pause
    exit /b 1
)

REM ── Load ALL variables from .env ─────────────────────────────────────────
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
    if not "%%A"=="" set "%%A=%%B"
)

REM ── Override Docker hostnames → localhost for local dev ───────────────────
set LLM_BASE_URL=http://localhost:1234/v1
set PGVECTOR_DSN=postgresql://rca:rca@localhost:5432/rca_db
set SPLUNK_HEC_URL=http://localhost:8088/services/collector/event
set SPLUNK_REST_URL=https://localhost:8089
set LOKI_URL=http://localhost:3100
set MCP_SERVER_URL=http://localhost:8001
set LOG_GENERATOR_URL=http://localhost:8090
set FRONTEND_DIR=%SCRIPT_DIR%frontend

REM ── PYTHONPATH = project root ─────────────────────────────────────────────
set PYTHONPATH=%SCRIPT_DIR%

echo   MCP server : %MCP_SERVER_URL%
echo   pgvector   : %PGVECTOR_DSN%
echo   Frontend   : %FRONTEND_DIR%
echo.
echo   Starting...  http://localhost:8000
echo   Press Ctrl+C to stop.
echo.

cd /d "%SCRIPT_DIR%"
"%VENV_UVICORN%" api_server.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo   API server stopped.
pause
