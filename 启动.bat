@echo off
setlocal
rem Windsong Lyre tool launcher (ASCII-only; real logic in launcher.ps1)
rem launcher.ps1 auto-relaunches as admin once (UAC) to fix blocked input.
cd /d "%~dp0"
rem Clear the downgrade flag left by a previous cancelled UAC prompt.
set "WINDSONG_NO_ELEVATION="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" %*
if errorlevel 1 (
  echo.
  echo [launch failed] press any key to close
  pause >nul
)
