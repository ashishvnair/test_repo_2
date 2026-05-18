@echo off
echo.
echo ========================================
echo   RCA Platform — Empty Vector Store
echo ========================================
echo.
echo This will DELETE all stored RCA reports from pgvector.
echo.
set /p confirm=Are you sure? (Y/N):
if /i not "%confirm%"=="Y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
echo Clearing vector store...
curl -s -X POST http://localhost:8000/api/vectordb/reset ^
     -H "Content-Type: application/json" ^
     -d "{}" > %TEMP%\reset_result.txt 2>&1

type %TEMP%\reset_result.txt
echo.

findstr /i "deleted\|success\|count\|ok" %TEMP%\reset_result.txt >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Vector store cleared successfully.
) else (
    echo [?]  Check output above - if you see a JSON response the reset worked.
)

echo.
echo You can now run a fresh RCA and store a new report.
echo.
pause
