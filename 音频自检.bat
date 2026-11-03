@echo off
chcp 936 >nul
cd /d "%~dp0"
title 风物之诗琴 - 音频自检
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 还没装运行环境，请先双击「启动.bat」。
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m piano_tool.audio_selfcheck
echo.
pause
