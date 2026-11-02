@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  云端：把本机 PawBot 的 content 导出到同步包
echo ============================================
echo.
echo 前提：本机 PawBot 已停止。
echo.
py -3.13 scripts\scp_sync.py --mode local --action push --root "%~dp0"
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
  echo [OK] 已导出到 D:\PawBotSync\pawbot-content.tgz
) else (
  echo [未完成] 退出码 %RC%，请看上面的提示。
)
echo.
pause
