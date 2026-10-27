@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\chat_window.py" %*
) else (
  py "tools\chat_window.py" %*
)
if errorlevel 1 pause
