@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv not found.
    echo     Create it first:
    echo         py -3.11 -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

if not exist ".env" (
    echo [!] .env not found - copy .env.example to .env and fill TELEGRAM_BOT_TOKEN / ALLOWED_USER_IDS.
    echo     Dry run is available without .env: run_bot.bat --once --dry-run
)

".venv\Scripts\python.exe" -m app.main %*
set EXITCODE=%ERRORLEVEL%
endlocal & exit /b %EXITCODE%
