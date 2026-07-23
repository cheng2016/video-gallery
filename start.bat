@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Pure ASCII launcher for Windows cmd.exe

set "PY="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 set "PY=python"
)

if not defined PY (
  echo [ERROR] Python not found.
  echo Install: https://www.python.org/downloads/
  echo Enable: Add python.exe to PATH
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] Creating virtualenv ...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] venv failed
    pause
    exit /b 1
  )
  echo [2/2] Installing Flask ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe missing
  pause
  exit /b 1
)

if not exist "app.py" (
  echo [ERROR] app.py not found in %cd%
  pause
  exit /b 1
)

echo.
echo Starting local video gallery ...
echo URL: http://127.0.0.1:8765
echo Close this window to stop the server.
echo.

".venv\Scripts\python.exe" -u app.py %*

set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" (
  echo [ERROR] exited with code %EC%
)
pause
endlocal
