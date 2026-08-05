@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "dist\VideoGallery\VideoGallery.exe" (
  echo Build first: build.bat
  pause
  exit /b 1
)
cd /d "%~dp0dist\VideoGallery"
echo Running with log capture...
VideoGallery.exe %* > "%~dp0dist\VideoGallery\run.log" 2>&1
echo Exit code %ERRORLEVEL%>> "%~dp0dist\VideoGallery\run.log"
echo.
echo Done. Opening run.log / startup.log
if exist run.log notepad run.log
if exist startup.log notepad startup.log
pause
endlocal