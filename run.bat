@echo off
REM Parley web server launcher. Reads .env (via app._load_dotenv) for PARLEY_* secrets.
REM Host/port are CLI args to uvicorn, so we parse them here too (defaults below).
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PARLEY_HOST=127.0.0.1"
set "PARLEY_PORT=8800"

if not exist ".env" (
  echo [parley] No .env found. Copy .env.example to .env and set PARLEY_TOKEN.
  echo [parley]   copy .env.example .env
  pause
  exit /b 1
)

REM Pull only HOST/PORT from .env for the uvicorn command line.
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  set "k=%%A"
  set "v=%%B"
  if /i "!k!"=="PARLEY_HOST" if not "!v!"=="" set "PARLEY_HOST=!v!"
  if /i "!k!"=="PARLEY_PORT" if not "!v!"=="" set "PARLEY_PORT=!v!"
)

set "PARLEY_HOST=!PARLEY_HOST:"=!"
set "PARLEY_PORT=!PARLEY_PORT:"=!"

echo [parley] http://!PARLEY_HOST!:!PARLEY_PORT!  (Ctrl+C to stop)
python -m uvicorn parley.web.app:app --host !PARLEY_HOST! --port !PARLEY_PORT! %*

REM If uvicorn exits (error or Ctrl+C), keep the window open so errors stay visible.
echo.
echo [parley] server exited with code %ERRORLEVEL%.
pause
endlocal
