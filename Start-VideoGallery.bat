@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "VideoGallery.exe" (
  echo [ERROR] VideoGallery.exe not found
  echo Keep this bat next to VideoGallery.exe and _internal\
  pause
  exit /b 1
)
if not exist "_internal\" (
  echo [ERROR] Missing _internal\ folder
  pause
  exit /b 1
)

echo ========================================
echo  Local Video Gallery
echo  URL: http://127.0.0.1:8765
echo  Close this window to stop.
echo ========================================
echo.
echo Tip: winget install ffmpeg
echo LAN share: VideoGallery.exe --lan
echo If it exits immediately, check startup.log
echo.

VideoGallery.exe %*
set "EC=%ERRORLEVEL%"
echo.
echo [exited] code=%EC%
if exist "startup.log" (
  echo ---- startup.log ----
  type "startup.log"
  echo ---------------------
)
if not "%EC%"=="0" (
  echo Failed. Common causes:
  echo  - Port 8765 already in use
  echo  - Missing _internal folder
)
echo.
pause
endlocal