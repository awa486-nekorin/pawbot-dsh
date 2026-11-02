@echo off
cd /d "%~dp0"
echo ============================================
echo  把本机人设/记忆推回云端
echo ============================================
echo.
echo 前提：本机 PawBot 已停止、MC 已退出。
echo.
py -3.13 scripts\scp_sync.py --mode remote --action push --root "%~dp0"
set RC=%ERRORLEVEL%
echo.
if %RC%==0 (
  echo [OK] 已推送到 ECS 的 /srv/pawbot-sync/pawbot-content.tgz
) else (
  echo [未完成] 退出码 %RC%，请看上面的提示。
)
echo.
pause
