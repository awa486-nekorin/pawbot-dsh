@echo off
cd /d "%~dp0"
echo ============================================
echo  PawBot 数据同步状态
echo ============================================
echo.
py -3.13 scripts\scp_sync.py --mode remote --action status --root "%~dp0"
echo.
pause
