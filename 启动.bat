@echo off
rem Windsong Lyre tool launcher (ASCII-only; real logic in launcher.ps1)
rem launcher.ps1 will auto-relaunch as admin once (UAC) to fix blocked input.
cd /d "%~dp0"
rem 每次双击都重新尝试提权：清掉上次拒绝 UAC 时留下的降级标记
set "WINDSONG_NO_ELEVATION="
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" %*
if errorlevel 1 (
  echo.
  echo [launch failed] press any key to close
  pause >nul
)
