@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM Build Windows onedir package (no Python needed for end users)

set "PY="
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where py >nul 2>&1
  if %ERRORLEVEL%==0 set "PY=py -3"
  if not defined PY (
    where python >nul 2>&1
    if %ERRORLEVEL%==0 set "PY=python"
  )
)

if not defined PY (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  pause
  exit /b 1
)

echo [1/3] Ensure build deps ...
%PY% -m pip install -q -r requirements-dev.txt
if errorlevel 1 (
  echo [ERROR] pip install failed
  pause
  exit /b 1
)

echo [2/3] PyInstaller onedir ...
%PY% -m PyInstaller --noconfirm --clean video_gallery.spec
if errorlevel 1 (
  echo [ERROR] PyInstaller failed
  pause
  exit /b 1
)

echo [3/3] Copy launcher ...
copy /Y "Start-VideoGallery.bat" "dist\VideoGallery\Start-VideoGallery.bat" >nul

echo.
echo Done: dist\VideoGallery\
echo.
echo IMPORTANT: keep VideoGallery.exe and _internal\ together.
echo Prefer: double-click Start-VideoGallery.bat  (not only the exe)
echo If exe flashes and closes, usually port 8765 is already in use -
echo close the other black window, then try again.
echo ffmpeg is NOT bundled - recommend: winget install ffmpeg
echo.
pause
endlocal