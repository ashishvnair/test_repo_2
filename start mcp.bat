@echo off
echo.
echo ========================================
echo   RCA Platform -- MCP Server  :8001
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
REM    This picks up EMBED_DIMS, SPLUNK_INDEX, LLM_CHAT_MODEL, EMBED_MODEL,
REM    LLM_API_KEY, SPLUNK_PASSWORD, SPLUNK_HEC_TOKEN and everything else.
REM    eol=# skips comment lines.  tokens=1,* delims== splits on first = only.
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%SCRIPT_DIR%.env") do (
    if not "%%A"=="" set "%%A=%%B"
)

REM ── Override Docker hostnames → localhost for local dev ───────────────────
REM    .env has host.docker.internal for LLM (used by containers).
REM    Running locally, LM Studio is on localhost:1234.
REM    pgvector and Splunk are in Docker but expose ports to localhost.
set LLM_BASE_URL=http://localhost:1234/v1
set PGVECTOR_DSN=postgresql://rca:rca@localhost:5432/rca_db
set SPLUNK_HEC_URL=http://localhost:8088/services/collector/event
set SPLUNK_REST_URL=https://localhost:8089
set LOKI_URL=http://localhost:3100

REM ── PYTHONPATH = project root (mcp_server uses relative imports) ──────────
set PYTHONPATH=%SCRIPT_DIR%

echo   EMBED_DIMS : %EMBED_DIMS%
echo   LLM        : %LLM_BASE_URL%
echo   pgvector   : %PGVECTOR_DSN%
echo   Splunk     : %SPLUNK_REST_URL%
echo   Index      : %SPLUNK_INDEX%
echo.
echo   Starting...  http://localhost:8001
echo   Press Ctrl+C to stop.
echo.

cd /d "%SCRIPT_DIR%"
"%VENV_UVICORN%" mcp_server.server:app --host 127.0.0.1 --port 8001 --reload

echo.
echo   MCP server stopped.
pause
