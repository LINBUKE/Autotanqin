@echo off
rem ============================================================
rem  Windsong Lyre Tool - entry point
rem  All detection / install / Chinese prompts are handled by
rem  launcher.ps1 (robust against Store stubs, broken venvs and
rem  incompatible Python versions).
rem  Optional args: web (web console), menu (text menu)
rem ============================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" %*
exit /b %errorlevel%
