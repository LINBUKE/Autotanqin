@echo off
rem Windsong Lyre tool launcher (ASCII-only; real logic in launcher.ps1)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" %*
if errorlevel 1 (
  echo.
  echo [launch failed] press any key to close
  pause >nul
)
