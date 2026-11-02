@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  云端：同步状态
echo ============================================
echo.
py -3.13 scripts\scp_sync.py --mode local --action status --root "%~dp0"
echo.
pause
